"""Explicit activation identity fixtures; no Native/model/auth/user raw proof."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tests.backend import test_summary_compatibility as fixtures
from workbench.backend import cli, summary_compat_identity as identity
from workbench.backend import summary_compat_runtime as runtime
from workbench.backend import summary_compatibility as compatibility
from workbench.backend.launcher import load_model_table, selector_level, selector_model
from workbench.backend.paths import DataLayout
from workbench.backend.raw_rotation import RawRotationPolicy


ROOT = Path(__file__).resolve().parents[2]
REAL_COMPATIBILITY = compatibility.SummaryCompatibility


class ActivationIdentityIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DVL10_INDEPENDENT_OUTPUT") is None:
            raise unittest.SkipTest("DVL10_INDEPENDENT_OUTPUT required; skipped tests are not a gate pass")

    def setUp(self):
        output = Path(os.environ["DVL10_INDEPENDENT_OUTPUT"])
        self.assertTrue(output.is_absolute() and output.is_dir())
        self.fixture = fixtures.CompatibilityCoreTests()
        # Existing DataLayout enforces UNIX socket path length. Use its usual
        # short owned temporary fixture; logs/reports stay in the output root.
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.layout = DataLayout(self.root)
        self.binary = self.root / "owner-bin" / "omp"
        self.binary.parent.mkdir(mode=0o700)
        self.binary.write_bytes(b"owned fake executable identity; never executed")
        self.binary.chmod(0o700)
        self.environment = {"PATH": str(self.binary.parent), "PI_PACKAGE_DIR": str(self.root / "package"),
                            "OMP_NATIVE_LIBRARY_PATH": ""}
        self.fixture.omp.update(identity.current_omp_identity(self.binary, version="fixture-version",
            instructions="fixture bundled definitions", environment=self.environment))
        self.fixture.service = REAL_COMPATIBILITY(self.layout.summary_compatibility,
            identity=lambda: deepcopy(self.fixture.ident), omp_identity=self.cheap,
            boundary=lambda: deepcopy(self.fixture.boundary))
        self.reference, self.record = self.fixture.fixture_verified_record()

    def cheap(self):
        return identity.current_omp_identity(self.binary, environment=self.environment)

    def invoke(self, *, yes=True, reference=None, getter=None):
        def construct(directory, **kwargs):
            return REAL_COMPATIBILITY(directory, identity=lambda: deepcopy(self.fixture.ident), **kwargs)
        supplied = {"result": "pass", "cleanup_complete": True, "omp": deepcopy(self.record["omp"])}
        with patch.object(compatibility, "SummaryCompatibility", side_effect=construct), \
             patch.object(cli, "current_omp_identity", side_effect=self.cheap), \
             patch.object(cli, "compatibility_boundary", side_effect=lambda: deepcopy(self.fixture.boundary)), \
             patch.object(identity, "observe_configured", side_effect=getter or (lambda *a, **k: deepcopy(supplied))) as observe, \
             patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            args = ["logs", "raw-rotation", "activate", "--evidence", reference or self.reference,
                    "--data-dir", str(self.root)]
            result = cli.main(args + (["--yes"] if yes else []))
        return result, observe.call_count

    def test_explicit_yes_getter_once_publishes_only_owned_policy(self):
        record_path = self.layout.summary_compatibility / (self.reference + ".json")
        original = record_path.read_bytes()
        self.assertTrue(self.fixture.service.activation_preflight(self.reference))
        self.assertFalse(self.fixture.service.verify_reference(self.reference), "cheap unknowns are not historical values")
        result, calls = self.invoke()
        self.assertEqual((result, calls), (0, 1))
        policy = json.loads(self.layout.raw_rotation.read_bytes())
        self.assertTrue(policy["activated"])
        self.assertEqual(policy["evidence"], self.reference)
        self.assertEqual(self.layout.raw_rotation.stat().st_mode & 0o777, 0o600)
        self.assertEqual(record_path.read_bytes(), original)
        self.assertFalse(self.layout.raw_v2.exists())

    def test_cheap_refusals_never_call_getter_or_publish_policy(self):
        self.assertEqual(self.invoke(yes=False), (1, 0))
        self.assertEqual(self.invoke(reference="not-a-record"), (1, 0))
        self.fixture.service.protect({"reason": "confirmed_safety_failure"})
        self.assertEqual(self.invoke(), (1, 0))
        self.fixture.persist_fixture(self.reference, deepcopy(self.record))
        self.fixture.ident["closure_digest"] = "f" * 64
        self.assertEqual(self.invoke(), (1, 0))
        self.assertFalse(self.layout.raw_rotation.exists())

    def test_legacy_assetless_record_can_operate_but_cannot_first_activate(self):
        record = deepcopy(self.record)
        for omp in (record["omp"], record["observations"][-1]["omp"]):
            omp.pop("binary_path")
            omp.pop("asset_selection")
        self.fixture.persist_fixture(self.reference, record)
        self.assertTrue(self.fixture.service.admission())
        self.assertFalse(self.fixture.service.status()["current_omp_exact"])
        self.assertFalse(self.fixture.service.verify_reference(self.reference))
        self.assertEqual(self.invoke(), (1, 0))
        self.assertFalse(self.layout.raw_rotation.exists())

    def test_unavailable_getter_or_changed_install_never_retries_or_publishes(self):
        original = self.binary.read_bytes()
        for mode in ("timeout", "cleanup", "binary", "assets"):
            with self.subTest(getter_boundary=mode):
                self.binary.write_bytes(original)
                self.environment["PI_PACKAGE_DIR"] = self.record["omp"]["asset_selection"]["PI_PACKAGE_DIR"]
                def getter(*args, **kwargs):
                    row = {"result": "pass", "cleanup_complete": True, "omp": deepcopy(self.record["omp"])}
                    if mode == "timeout":
                        row.update(result="inconclusive", error="TimeoutError")
                    elif mode == "cleanup":
                        row["cleanup_complete"] = False
                    elif mode == "binary":
                        self.binary.write_bytes(b"actual owned binary changed during getter")
                    else:
                        self.environment["PI_PACKAGE_DIR"] = str(self.root / "other-package")
                    return row
                self.assertEqual(self.invoke(getter=getter), (1, 1))
                self.assertFalse(self.layout.raw_rotation.exists())

    def test_install_change_after_preliminary_gate_is_rechecked_under_lock(self):
        from workbench.backend.paths import InstanceLock
        acquire = InstanceLock.acquire
        locks = []
        def acquire_and_replace(lock):
            result = acquire(lock)
            # Actual owned install replacement at an OS lock boundary, without
            # replacing the preliminary, locked guard or signal mechanisms.
            locks.append(str(lock.path))
            if lock.path == self.layout.raw_rotation.with_suffix(".lock"):
                self.binary.write_bytes(b"owned replacement after preliminary verification")
            return result
        with patch.object(InstanceLock, "acquire", acquire_and_replace):
            result, calls = self.invoke()
        self.assertEqual((result, calls), (1, 1))
        self.assertIn(str(self.layout.raw_rotation.with_suffix(".lock")), locks)
        self.assertFalse(self.layout.raw_rotation.exists())

    def test_status_reads_and_ordinary_admission_never_launch_getter(self):
        with patch.object(identity, "observe_configured", side_effect=AssertionError("ordinary getter")), \
             patch("subprocess.Popen", side_effect=AssertionError("ordinary Native")):
            self.assertTrue(self.fixture.service.admission())
            self.assertFalse(self.fixture.service.verify_reference(self.reference))
            self.assertIsNotNone(self.fixture.service._verified_record(self.reference))
            self.assertFalse(self.fixture.service.observed_status()["current_omp_exact"])
            status = RawRotationPolicy(self.layout.raw_rotation, self.fixture.service).status()
            self.assertTrue(status["observationOnly"])
            self.assertFalse(status["activated"])

    def test_real_getter_wiring_selects_owner_binary_isolates_and_cleans(self):
        table = load_model_table()
        manager, worker = table["manager"]["models"]["default"], table["worker"]["models"]["default"]
        def model(selector):
            provider, identifier = selector_model(selector).split("/", 1)
            return {"provider": provider, "id": identifier, "api": "openai-responses",
                    "baseUrl": "https://example.invalid/v1", "contextWindow": 131072, "maxTokens": 8192}
        metadata = deepcopy(self.record["model_metadata"])
        metadata.update(model=model(manager), thinkingLevel=selector_level(manager), outputCap=8192)
        metadata["childSelection"].update(model=model(worker), selector=worker, thinkingLevel=selector_level(worker),
            modelTableDigest=sha256(json.dumps(table, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
        real_mkdtemp = tempfile.mkdtemp
        original = self.binary.read_bytes()
        for mode in ("normal", "changed_binary", "timeout"):
            with self.subTest(readback_boundary=mode), ExitStack() as stack:
                self.binary.write_bytes(original)
                roots, processes, closed = [], [], []
                def owned_root(*args, **kwargs):
                    path = Path(real_mkdtemp(*args, dir=self.root, **kwargs))
                    roots.append(path)
                    self.addCleanup(shutil.rmtree, path, True)
                    return str(path)
                class NoInput(BytesIO):
                    def write(inner, data):
                        raise AssertionError("metadataOnly must not submit input")
                    def close(inner):
                        closed.append(True)
                        super().close()
                def launch(argv, **options):
                    process = type("ControlledNative", (), {})()
                    process.pid, process.returncode, process.stdin = 99999991, None, NoInput()
                    process.poll = lambda: process.returncode
                    processes.append((process, argv, options))
                    root = Path(options["cwd"]).parent
                    self.assertEqual(json.loads((root / "fixture.json").read_bytes())["metadataOnly"], True)
                    if mode != "timeout":
                        (root / "completed.json").write_text(json.dumps({"metadata": metadata,
                            "version": "fixture-readback", "definitionDigest": "b" * 64}))
                    if mode == "changed_binary":
                        self.binary.write_bytes(b"actually changed owned fake executable")
                    return process
                def stop(process, ref):
                    self.assertEqual(ref["start_ticks"], "owned-fixture-ticks")
                    process.returncode = 0
                    return None
                stack.enter_context(patch.object(identity.tempfile, "mkdtemp", owned_root))
                stack.enter_context(patch.object(identity.subprocess, "Popen", side_effect=launch))
                stack.enter_context(patch.object(runtime, "_identity", return_value="owned-fixture-ticks"))
                stack.enter_context(patch.object(runtime, "_stop_leader", side_effect=stop))
                stack.enter_context(patch.object(runtime, "_remaining", return_value=[]))
                if mode == "timeout":
                    stack.enter_context(patch.object(identity.time, "monotonic", side_effect=[0, 51]))
                supplied = {**self.environment, "OPENAI_API_KEY": "fixture-secret-never-forward",
                            "PI_CONFIG_DIR": "/unowned/config", "LD_PRELOAD": "/unowned/library"}
                row = identity.observe_configured(ROOT, environment=supplied)
                self.assertEqual(len(processes), 1)
                self.assertTrue(row["cleanup_complete"])
                self.assertTrue(closed)
                self.assertTrue(all(not path.exists() for path in roots))
                _, argv, options = processes[0]
                self.assertIn(str(self.binary.resolve()), argv)
                self.assertEqual(options["env"]["PATH"], "/usr/local/bin:/usr/bin:/bin")
                for key in ("PI_PACKAGE_DIR", "OMP_NATIVE_LIBRARY_PATH"):
                    self.assertEqual(options["env"][key], self.environment[key])
                for key in ("OPENAI_API_KEY", "LD_PRELOAD"):
                    self.assertNotIn(key, options["env"])
                self.assertNotEqual(options["env"]["PI_CONFIG_DIR"], supplied["PI_CONFIG_DIR"])
                self.assertNotIn("fixture-secret-never-forward", json.dumps(row))
                if mode == "normal":
                    self.assertEqual(row["result"], "pass")
                    self.assertEqual(row["omp"]["binary_sha256"], sha256(original).hexdigest())
                    self.assertEqual(row["omp"]["asset_selection"], self.record["omp"]["asset_selection"])
                else:
                    self.assertEqual(row["result"], "inconclusive")
                    if mode == "changed_binary":
                        self.assertIs(row["checks"]["binary_epoch_stable"], False)
                    else:
                        self.assertEqual(row["error"], "TimeoutError")
