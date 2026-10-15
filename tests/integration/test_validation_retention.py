"""Independent DVL13 wiring fixtures; no Native, 300s, volume or paid proof."""
from contextlib import redirect_stdout
import ast
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from tests.backend import test_summary_compatibility as records
from tests.dev_validation import feature_items
from tests.integration import live_raw_activation as product
from tests.integration import live_validation_retention as harness
from tools import dev_validate as runner
from workbench.backend.paths import write_private_json
from workbench.backend.summary_compat_identity import compatibility_boundary, current_omp_identity
from workbench.backend.summary_compatibility import collect_runtime_identity


ROOT = Path(__file__).resolve().parents[2]
ORIGIN_FACTS = ("task_grant_bound", "command_start_bound", "work_source_ranges_bound",
                "terminal_close_bound", "unrelated_scope_rejected", "notice_not_authority",
                "user_role_not_authority", "pause_revoke_preserved", "end_once_no_replay")


class FeatureIntegrationIndependentTests(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ["DVL10_INDEPENDENT_OUTPUT"])
        self.assertTrue(output.is_absolute() and output.is_dir())
        temporary = tempfile.TemporaryDirectory(prefix="feature-unit-", dir=output)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / "selected-engine"
        self.binary.write_bytes(b"owned placeholder identity, never executed")
        self.binary.chmod(0o700)
        self.environment = patch.dict(os.environ, {"WB_DVL_FEATURE_EVIDENCE": str(self.root),
            "WB_DVL_INVOCATION_ID": "independent-invocation", "WB_DVL_OMP": str(self.binary)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_runtime_notice_request_uses_supported_worker_delivery_schema(self):
        # Evaluate the actual caller expression without importing/running the
        # 300-second/Native harness body. Expected fields come from the public
        # worker notice contract, exercised in bridge handoff_tools tests.
        from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog
        subject = {"schemaVersion": 1, "kind": "command", "id": str(uuid4())}
        sink = ActiveSubjectLog(ImportantLogCatalog(self.root / "important", create=True))
        self.assertFalse(sink.authority(subject, active=True, paused=False,
            revoked=False, has_new_input=True)["allowed"], "a notice is not a confirmed command start")
        tree = ast.parse(Path(harness.__file__).read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "_notice"]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        role = ast.literal_eval(call.args[0])
        payload = call.args[1]
        if isinstance(payload, ast.Name):
            assignments = [node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == payload.id
                                   for target in node.targets)]
            self.assertEqual(len(assignments), 1)
            payload = assignments[0]
        self.assertIsInstance(payload, ast.Dict, "inspect only the actual simple notice dict assignment")
        notice = eval(compile(ast.Expression(payload), str(harness.__file__), "eval"),
                      {"notice_subject": subject, "uuid4": uuid4})
        self.assertEqual(role, "worker")
        self.assertIn(notice.get("type"), {"terminal_check", "terminal_done", "status_check"},
                      "the actual harness notice must be a supported worker positive delivery control")
        self.assertEqual(str(UUID(notice["notice_id"])), notice["notice_id"])

    def storage_port(self):
        # Only the synchronous public storage ports are initialized. This is
        # an actual Backend object, without boot/bridge/terminal/model launch.
        from workbench.backend.service import Backend
        from workbench.storage.important_log import ImportantLogCatalog
        backend = Backend.__new__(Backend)
        backend.important_catalog = ImportantLogCatalog(self.root / "storage", create=True,
            target_bytes=2_000_000, low_water_bytes=1_000_000)
        backend.layout = SimpleNamespace(important_logs=backend.important_catalog.directory)
        backend.important_storage_status = {}
        backend.automation_loop = None
        backend.automation = {"state": "idle"}
        subject = {"schemaVersion": 1, "kind": "task", "id": str(uuid4())}
        written = backend.important_catalog.append(subject, "event", [], {"schemaVersion": 1,
            "facts": ["original owned Task storage fixture"], "interpretations": [], "unknowns": []})
        self.assertEqual(written["state"], "ok")
        return backend, subject

    def test_storage_controls_use_actual_catalog_and_keep_pressure_non_authoritative(self):
        from workbench.storage.important_log import ActiveSubjectLog, ImportantLogReader
        backend, subject = self.storage_port()
        catalog = backend.important_catalog
        original_targets = catalog.target_bytes, catalog.low_water_bytes
        original_active = catalog.active_uuid()
        queries = []
        real_show = ImportantLogReader.show
        def readonly_show(reader, selected, **kwargs):
            # LG-D18 permits active SQLite sidecar creation/read marks. The
            # retained archive body must remain byte-identical through query.
            archive = catalog.directory / (original_active + ".sqlite3")
            before = sha256(archive.read_bytes()).hexdigest()
            page = real_show(reader, selected, **kwargs)
            after = sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(after, before, "readonly query changed the retained archive body")
            self.assertTrue(page["complete"])
            self.assertTrue(all(row["entry"]["subject"] == selected for row in page["entries"]))
            queries.append({"subject": selected, "archive": archive.name, "archive_sha256": before,
                            "entry_count": len(page["entries"])})
            return page
        with patch.object(ImportantLogReader, "show", new=readonly_show), \
             patch("subprocess.Popen", side_effect=AssertionError("Native/model launch forbidden")):
            result = harness._storage_controls(backend, subject)
        self.assertEqual((catalog.target_bytes, catalog.low_water_bytes), original_targets)
        self.assertEqual(result["targets_restored"], list(original_targets))
        self.assertEqual(result["checks"], {"actual_db_rollover_warning": True,
            "archive_readonly_hash": True, "active_db_delete_refused": True,
            "explicit_archive_delete_recount": True, "storage_failure_explicit": True,
            "storage_recovery_without_resume": True})
        pressure = result["pressure_fixture"]
        self.assertEqual(pressure["evidence_level"], "fixture")
        self.assertNotEqual(pressure["subject"], subject)
        self.assertEqual(pressure["before"]["task_entry_count"], 1)
        self.assertEqual(pressure["after_original_db"]["task_entry_count"], 1)
        self.assertTrue(pressure["appends"])
        self.assertTrue(all(row["state"] == "ok" and row["entry_bytes"] > 0
                            and isinstance(row["prune"], dict) for row in pressure["appends"]))
        self.assertEqual(pressure["appends"][-1]["rollover"]["from"], original_active)
        self.assertNotEqual(catalog.active_uuid(), original_active)
        self.assertFalse((catalog.directory / (original_active + ".sqlite3")).exists())
        self.assertEqual(backend.important_storage_status["databaseCount"], result["status"]["databaseCount"])
        self.assertEqual(backend.important_storage_status["countState"], "known")
        self.assertEqual(result["status"]["databaseCount"], 1)
        self.assertFalse(result["status"]["cleanupNeeded"])
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["subject"], pressure["subject"])
        self.assertGreater(queries[0]["entry_count"], 0)
        self.assertEqual(result["contention_state"], "busy")
        self.assertEqual(result["recovery"]["state"], "healthy")
        self.assertEqual(backend.automation, {"state": "idle"})
        authority = ActiveSubjectLog(catalog).authority(pressure["subject"], active=True,
            paused=False, revoked=False, has_new_input=True)
        self.assertFalse(authority["allowed"], "event pressure creates no execution authorization")
        self.assertEqual(result["retention_evidence_level"], "fixture")
        print("storage_controls_actual=" + json.dumps({"pressure": pressure, "checks": result["checks"],
            "delete": result["delete"], "retention": result["retention"], "recovery": result["recovery"]["state"],
            "targetsRestored": result["targets_restored"], "readonlyQueries": queries,
            "pressureAuthority": authority, "expiryDeletionClaim": False}, sort_keys=True))

    def test_storage_controls_restore_targets_after_real_maintenance_lock_contention(self):
        import fcntl
        import sqlite3
        backend, subject = self.storage_port()
        catalog = backend.important_catalog
        original_targets = catalog.target_bytes, catalog.low_water_bytes
        original_active = catalog.active_uuid()
        lock = os.open(catalog.directory / "maintenance.lock", os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("subprocess.Popen", side_effect=AssertionError("Native/model launch forbidden")):
                with self.assertRaisesRegex(sqlite3.OperationalError, "busy"):
                    harness._storage_controls(backend, subject)
            self.assertEqual((catalog.target_bytes, catalog.low_water_bytes), original_targets)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
        self.assertEqual(catalog.active_uuid(), original_active)
        self.assertEqual(catalog.recovery_probe(force=True)["state"], "healthy")
        self.assertEqual(backend.automation, {"state": "idle"})
        print("storage_controls_real_lock_failure=" + json.dumps({"failure": "OperationalError:busy",
            "boundary": "catalog active identity lease before pressure; no injected helper error",
            "targetsRestored": list(original_targets), "activeUnchanged": True, "afterUnlock": "healthy"}))

    def producer(self):
        fixture = records.CompatibilityCoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        retained = self.root / "product"
        fixture.ident = collect_runtime_identity(ROOT)
        fixture.boundary = compatibility_boundary()
        fixture.omp = current_omp_identity(self.binary, version="fixture-observed-version",
            instructions="fixture-complete-bundle")
        fixture.service.directory = retained
        reference, record = fixture.fixture_verified_record()
        # The actual producer's bounded log is JSON. Keep this synthetic log
        # valid JSON too; its file hash and original record binding stay real.
        log = retained / record["evidence"][0]["path"]
        log.write_bytes(b'{"fixture_only":true,"unknown_native_io":null}\n')
        record["evidence"][0]["sha256"] = sha256(log.read_bytes()).hexdigest()
        record["io"] = {"network_requests": None, "model_calls": None}
        fixture.persist_fixture(reference, record)
        _, invocation, candidate = product.feature_context()
        envelope = {"invocation": invocation, "candidate": candidate, "record_id": reference,
            "selected_binary": {"binary_path": str(self.binary.resolve()),
                                "binary_sha256": sha256(self.binary.read_bytes()).hexdigest()},
            "selected_binary_after": {"binary_path": str(self.binary.resolve()),
                                      "binary_sha256": sha256(self.binary.read_bytes()).hexdigest()},
            "record_omp": deepcopy(record["omp"]),
            "result": "passed", "runtime_only": True,
            "activation": {"exit_code": 0, "warning_observed": True},
            "caveats": record["caveats"], "io": deepcopy(record["io"]), "artifacts": [
                {"path": name, "sha256": sha256((retained / name).read_bytes()).hexdigest()}
                for name in (reference + ".json", log.name, "state.json")]}
        product.write_json(self.root / "product-producer.json", envelope)
        return record, envelope, log

    def test_nonpass_record_and_referenced_log_survive_cleanup_without_activation(self):
        from workbench.backend import summary_compat_runtime as runtime
        from workbench.backend.summary_compatibility import ScriptedCompatibilityProvider
        observations = []
        for mode in ("inconclusive", "failed", "changed_log"):
            with self.subTest(mode=mode):
                case = self.root / mode
                case.mkdir(mode=0o700)
                work, data = case / "work", case / "data"
                work.mkdir(mode=0o700)
                data.mkdir(mode=0o700)
                fixture = records.CompatibilityCoreTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.service.directory = data / "summary-compatibility"
                partial = runtime.unavailable("owned_priority_cleanup_unverified")
                partial.update(level="failed" if mode == "failed" else "inconclusive",
                    observations=[{"kind": "owned_product_priority_partial", "cleanup_complete": False,
                                   "remaining": [123], "native_io_attribution": None}])
                with patch.object(ScriptedCompatibilityProvider, "observe", return_value=partial), \
                     patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")):
                    verified = fixture.service.verify_scripted()
                source = fixture.service.directory
                record_name = verified["id"] + ".json"
                record = product.read_json(source / record_name)
                self.assertEqual(record["observations"], partial["observations"])
                self.assertNotEqual(record["result"], "pass")
                log_name = record["evidence"][0]["path"]
                originals = {name: (source / name).read_bytes() for name in (record_name, log_name)}
                if mode == "changed_log":
                    (source / log_name).write_bytes(b'{"changed":true}\n')
                commands = []
                def cli(argv, **kwargs):
                    commands.append(argv)
                    self.assertIn("verify", argv)
                    self.assertNotIn("activate", argv)
                    return subprocess.CompletedProcess(argv, 1 if mode == "failed" else 2,
                                                       json.dumps(verified), "")
                with patch.dict(os.environ, {"WB_DVL_FEATURE_EVIDENCE": str(case)}), \
                     patch.object(product.subprocess, "run", side_effect=cli), \
                     patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")):
                    outcome = product.prepare_activation(work, data)
                self.assertEqual(outcome["result"], "failed" if mode == "failed" else "inconclusive")
                self.assertEqual(len(commands), 1)
                self.assertEqual(outcome["verification"], verified)
                self.assertFalse((case / "product-producer.json").exists())
                self.assertFalse((data / "raw-rotation.json").exists())
                archive = outcome["nonpass_archive"]
                self.assertEqual(archive["available"], mode != "changed_log")
                if archive["available"]:
                    retained = Path(archive["directory"])
                    self.assertEqual(retained.parent, case)
                    self.assertEqual(archive["original_result"], record["result"])
                    self.assertEqual({row["path"] for row in archive["artifacts"]}, set(originals))
                    shutil.rmtree(data)  # Only this fixture's original private data.
                    for row in archive["artifacts"]:
                        target = retained / row["path"]
                        self.assertEqual(target.parent, retained)
                        self.assertEqual(target.name, row["path"])
                        self.assertEqual(target.read_bytes(), originals[row["path"]])
                        self.assertEqual(row["sha256"], sha256(originals[row["path"]]).hexdigest())
                        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                        self.assertLessEqual(target.stat().st_size, 1_000_000)
                    observations.append({"mode": mode, "result": outcome["result"],
                                         "artifacts": archive["artifacts"], "sourceRemoved": True})
                else:
                    self.assertEqual(archive["reason"], "ValueError")
                    observations.append({"mode": mode, "archiveUnavailable": True,
                                         "result": outcome["result"]})
        print("nonpass_archive_observations=" + json.dumps(observations, sort_keys=True))

    def test_selected_alias_and_metadata_getter_ignore_competing_path_omp(self):
        competing = self.root / "competing"
        competing.mkdir(mode=0o700)
        other = competing / "omp"
        other.write_bytes(b"different competing executable, never run")
        other.chmod(0o700)
        selected = {"binary_path": str(self.binary.resolve()),
                    "binary_sha256": sha256(self.binary.read_bytes()).hexdigest()}
        with patch.dict(os.environ, {"PATH": str(competing) + ":/usr/bin:/bin"}):
            self.assertEqual(Path(shutil.which("omp")).resolve(), other.resolve())
            for mode in ("selected", "wrong_getter_identity"):
                with self.subTest(getter=mode):
                    work = self.root / ("metadata-" + mode)
                    def getter(source, *, environment):
                        self.assertEqual(source, ROOT)
                        self.assertEqual(Path(shutil.which("omp", path=environment["PATH"])).resolve(), self.binary.resolve())
                        observed = current_omp_identity(environment=environment)
                        self.assertEqual({key: observed[key] for key in selected}, selected)
                        if mode == "wrong_getter_identity":
                            observed["binary_sha256"] = sha256(other.read_bytes()).hexdigest()
                        return {"result": "pass", "cleanup_complete": True, "omp": observed,
                                "metadata": {"fixture_only": True}, "unknown_io": None}
                    with patch("workbench.backend.summary_compat_identity.observe_configured", side_effect=getter) as observe, \
                         patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")):
                        result = harness.run_model_metadata(work)
                    observe.assert_called_once()
                    self.assertEqual((work / "bin").stat().st_mode & 0o777, 0o700)
                    self.assertEqual((work / "bin/omp").resolve(), self.binary.resolve())
                    self.assertEqual(result["selected_binary"], selected)
                    self.assertEqual(result["selected_binary_after"], selected)
                    self.assertEqual(result["selected_binary_binding"], mode == "selected")
                    self.assertEqual(result["result"], "passed" if mode == "selected" else "inconclusive")
                    self.assertIsNone(result["observation"]["omp"]["version"])
                    self.assertIsNone(result["observation"]["omp"]["bundled_sha256"])
                    self.assertIsNone(result["io"]["native_internal_io"])

    def test_wrong_original_record_binary_cannot_use_operating_drift_as_strict_proof(self):
        record, envelope, _ = self.producer()
        retained = self.root / "product"
        record["omp"]["binary_sha256"] = "c" * 64
        record["observations"][-1]["omp"] = deepcopy(record["omp"])
        write_private_json(retained / (record["id"] + ".json"), record)
        state = product.read_json(retained / "state.json")
        state["record_sha256"] = records._digest(record)
        write_private_json(retained / "state.json", state)
        for artifact in envelope["artifacts"]:
            artifact["sha256"] = sha256((retained / artifact["path"]).read_bytes()).hexdigest()
        write_private_json(self.root / "product-producer.json", envelope)
        service = product.SummaryCompatibility(retained, identity=lambda: collect_runtime_identity(ROOT),
            omp_identity=lambda: current_omp_identity(self.binary), boundary=compatibility_boundary)
        self.assertTrue(service.admission(), "guarded operational drift remains allowed")
        self.assertEqual(service.status()["state"], "version_drift_unverified")
        with patch.object(product.SummaryCompatibility, "status", side_effect=AssertionError("binding must precede status")):
            with self.assertRaisesRegex(ValueError, "differs from selected executable"):
                product.consume_product()

    def test_changed_selected_file_cannot_use_operating_drift_as_strict_proof(self):
        self.producer()
        self.binary.write_bytes(b"changed selected executable, never run")
        service = product.SummaryCompatibility(self.root / "product", identity=lambda: collect_runtime_identity(ROOT),
            omp_identity=lambda: current_omp_identity(self.binary), boundary=compatibility_boundary)
        self.assertTrue(service.admission(), "operational policy is descriptive, not strict selected identity")
        self.assertEqual(service.status()["state"], "version_drift_unverified")
        with patch.object(product.SummaryCompatibility, "status", side_effect=AssertionError("binding must precede status")):
            with self.assertRaisesRegex(ValueError, "differs from selected executable"):
                product.consume_product()

    def test_same_invocation_consumer_revalidates_original_hashes_and_retains_unknowns(self):
        record, envelope, log = self.producer()
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        with patch("subprocess.Popen", side_effect=AssertionError("Native forbidden")):
            result = product.consume_product()
        self.assertEqual(result["result"], "passed", "synthetic consumer fixture, not runtime pass")
        self.assertTrue(all(result["checks"].values()))
        self.assertFalse(result["verify_reference"])
        self.assertFalse(result["status"]["current_omp_exact"])
        self.assertIsNone(result["status"]["current_omp"]["version"])
        self.assertEqual(result["io"], envelope["io"])
        self.assertEqual(result["caveats"], envelope["caveats"])
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes()
                                 for p in self.root.rglob("*") if p.is_file()}, "consumer is read-only")
        log.write_bytes(b'{"changed":true}\n')
        with self.assertRaisesRegex(ValueError, "changed product producer evidence"):
            product.consume_product()
        # Even a fresh outer file hash cannot replace the original record/log
        # relationship that the real product validator rechecks.
        next(row for row in envelope["artifacts"] if row["path"] == log.name)["sha256"] = sha256(log.read_bytes()).hexdigest()
        write_private_json(self.root / "product-producer.json", envelope)
        invalid = product.consume_product()
        self.assertEqual(invalid["result"], "inconclusive")
        self.assertFalse(invalid["checks"]["original_record_complete"])

    def test_absent_wrong_invocation_or_source_producer_never_admits(self):
        with self.assertRaises(FileNotFoundError):
            product.consume_product()
        _, envelope, _ = self.producer()
        for key in ("invocation", "candidate"):
            with self.subTest(stale_binding=key):
                bad = {**envelope, key: "different-current-binding"}
                write_private_json(self.root / "product-producer.json", bad)
                with self.assertRaisesRegex(ValueError, "stale product producer"):
                    product.consume_product()

    def test_composite_preserves_delegate_raw_and_individual_false_unknown_origin(self):
        raw = {"observations": [{"id": "Q01-route", "facts": {"observed": None},
                                 "evidence_ref": "original-owned-report.json"},
                                {"id": "Q01-origin", "facts": {"synthetic_all_true": True}}],
               "unknowns": ["delegate-native-io-unattributed"]}
        route = {"agent_definition_sha256": "a" * 64, "harness": "delegate-harness"}
        delegate = SimpleNamespace(prepare_route=lambda context: deepcopy(route),
            collect_observations=lambda: deepcopy(raw), close=lambda: None)
        facts = dict.fromkeys(ORIGIN_FACTS, True)
        facts["notice_not_authority"] = False
        facts.pop("user_role_not_authority")
        origin = {"facts": facts}
        origin_path = self.root / "backend-origin.json"
        product.write_json(origin_path, origin)
        runtime = harness._CompositeRuntime(delegate, origin, origin_path, "a" * 64, self.root)
        prepared = runtime.prepare_route({})
        expected = sha256((route["harness"] + sha256(Path(harness.__file__).read_bytes()).hexdigest()).encode()).hexdigest()
        self.assertEqual(prepared["harness"], "dvl13-composite-" + expected)
        projected = runtime.collect_observations()
        self.assertEqual(product.read_json(self.root / "delegate-envelope.json"), raw)
        self.assertEqual(projected["observations"][0], raw["observations"][0])
        self.assertEqual(projected["unknowns"], raw["unknowns"])
        origin_row = projected["observations"][-1]
        self.assertEqual(set(origin_row["facts"]), set(ORIGIN_FACTS))
        self.assertIs(origin_row["facts"]["notice_not_authority"], False)
        self.assertIsNone(origin_row["facts"]["user_role_not_authority"])
        self.assertEqual(origin_row["unknowns"], ["actual_backend_origin_unobserved:user_role_not_authority"])
        self.assertEqual(origin_row["evidence_ref"], str(origin_path))
        runtime.bundle = "b" * 64
        with self.assertRaisesRegex(ValueError, "fresh bundled definition"):
            runtime.prepare_route({})
        runtime.close()
        self.assertTrue(runtime.closed)

    def test_feature_report_preserves_failure_unknown_timeout_and_owned_cleanup(self):
        output = self.root / "fixture-output"
        output.mkdir(mode=0o700)
        for mode in ("passed", "failed", "inconclusive", "timeout"):
            with self.subTest(observation=mode):
                namespace = self.root / mode
                namespace.mkdir(mode=0o700)
                local_output = namespace / "output"
                local_output.mkdir(mode=0o700)
                def external(work):
                    work.mkdir(mode=0o700)
                    (work / "marker").write_bytes(b"owned fixture only")
                    if mode == "timeout":
                        raise TimeoutError("controlled external runtime deadline")
                    return {"result": mode, "raw_fact": None,
                            "cleanup": {"complete": True, "remaining_owned": []}}
                name = "feature-" + mode
                case = feature_items.FeaturePreciseItems()
                with patch.dict(os.environ, {"WB_DVL_FEATURE_ITEMS": "1", "WB_DVL_NAMESPACE": str(namespace),
                    "DVL10_INDEPENDENT_OUTPUT": str(local_output), "WB_DVL_PRECISE_TEST_OUTPUT": str(local_output)}), \
                     patch.object(feature_items.importlib, "import_module", return_value=SimpleNamespace(controlled=external)), \
                     patch.object(feature_items.h, "REPORT_DIR", namespace / "reports"), \
                     patch.object(feature_items.h, "_first_line", return_value="fixture-head"), redirect_stdout(StringIO()):
                    if mode == "passed":
                        case.run_feature(name, "controlled")
                    else:
                        with self.assertRaises(AssertionError if mode == "failed" else unittest.SkipTest):
                            case.run_feature(name, "controlled")
                report = json.loads((namespace / "reports" / name / (name + ".json")).read_bytes())
                self.assertEqual(report["steps"]["actual_execution"]["status"],
                    {"passed": "pass", "failed": "fail"}.get(mode, "not_run"))
                if mode != "timeout":
                    self.assertIsNone(report["observation"]["raw_fact"])
                    self.assertFalse((namespace / "feature").exists())
                else:
                    self.assertEqual(report["steps"]["owned_cleanup"]["status"], "not_run")
                    self.assertTrue((namespace / "feature").exists(), "unknown cleanup cannot be reported removed")
                item = runner.Item(name, ("DV-AC-04",), (), "fixture", scenario_steps={name: (
                    "actual_execution", "same_candidate", "owned_cleanup")})
                retained = namespace / "retained"
                retained.mkdir(mode=0o700)
                evidence = runner._scenario_evidence(item, 0, namespace, retained)
                self.assertEqual(evidence[0]["status"] == "passed", mode == "passed")

    def test_owned_child_environment_and_failure_finally_use_only_captured_identity(self):
        for mode in ("passed", "timeout"):
            with self.subTest(child=mode):
                work = self.root / ("child-" + mode)
                process = SimpleNamespace(pid=99999991, returncode=None)
                captured, signals, waits = [], [], []
                def launch(argv, **options):
                    captured.append((argv, options))
                    product.write_json(work / "result.json", {"result": "passed"})
                    return process
                def wait(timeout):
                    waits.append(timeout)
                    if mode == "timeout" and process.returncode is None:
                        raise subprocess.TimeoutExpired("controlled child", timeout)
                    process.returncode = 0 if mode == "passed" else process.returncode
                process.wait = wait
                def stop(pid, ticks):
                    signals.append((pid, ticks))
                    process.returncode = -9
                with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-secret-not-forwarded"}), \
                     patch.object(harness.subprocess, "Popen", side_effect=launch), \
                     patch.object(harness.h, "ticks", return_value=12345), \
                     patch.object(harness.h, "alive", side_effect=lambda pid, ticks: process.returncode is None), \
                     patch.object(harness.h, "kill_exact", side_effect=stop):
                    if mode == "passed":
                        self.assertEqual(harness._child(work), {"result": "passed"})
                    else:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            harness._child(work)
                self.assertEqual(len(captured), 1)
                _, options = captured[0]
                self.assertTrue(options["start_new_session"])
                self.assertEqual(options["env"]["HOME"], str(work))
                self.assertEqual(Path(shutil.which("omp", path=options["env"]["PATH"])).resolve(), self.binary.resolve())
                self.assertEqual(options["env"]["DVL10_INDEPENDENT_OUTPUT"], str(work))
                self.assertNotIn("OPENAI_API_KEY", options["env"])
                self.assertEqual(signals, [(process.pid, 12345)] if mode == "timeout" else [])
                self.assertIsNotNone(process.returncode)
                self.assertEqual(waits, [2250, 10] if mode == "timeout" else [2250])

    def test_runtime_cost_hierarchy_preserves_workload_and_one_terminal_deadline(self):
        phases = harness.RUNTIME_PHASE_SECONDS
        self.assertEqual(sum(phases.values()), 2020)
        self.assertEqual(harness.RUNTIME_CLEANUP_SECONDS, 216)
        self.assertEqual(harness.RUNTIME_SUPERVISOR_SECONDS, 2250)
        feature = next(item for item in runner.FEATURE_ITEMS if item.key == "new_storage_summary_raw_product_gates")
        cutoff = feature.expected_wait_seconds + runner.SCENARIO_IDLE_SECONDS
        self.assertEqual(cutoff, 2370)
        self.assertLess(sum(phases.values()) + harness.RUNTIME_CLEANUP_SECONDS, harness.RUNTIME_SUPERVISOR_SECONDS)
        self.assertLess(harness.RUNTIME_SUPERVISOR_SECONDS, cutoff)

        tree = ast.parse(Path(harness.__file__).read_text())
        runtime = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_runtime")
        assignments = {node.targets[0].id: node.value for node in ast.walk(runtime)
                       if isinstance(node, ast.Assign) and len(node.targets) == 1
                       and isinstance(node.targets[0], ast.Name)}
        count = ast.literal_eval(assignments["chunk_count"])
        command = eval(compile(ast.Expression(assignments["raw_command"]), str(harness.__file__), "eval"),
                       {"shlex": shlex, "chunk_count": count})
        program = ast.parse(shlex.split(command)[-1])
        payload = next(node.value for node in program.body if isinstance(node, ast.Assign)
                       and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "p")
        repetitions = next(node.args[0] for node in ast.walk(program) if isinstance(node, ast.Call)
                           and isinstance(node.func, ast.Name) and node.func.id == "range")
        self.assertEqual(len(eval(compile(ast.Expression(payload), "owned-raw-payload", "eval")))
                         * ast.literal_eval(repetitions), 529_527_850)
        raw_call = assignments["raw"]
        self.assertEqual(raw_call.func.id, "terminal")
        self.assertEqual(eval(compile(ast.Expression(raw_call.args[1]), str(harness.__file__), "eval"),
                              {"RUNTIME_PHASE_SECONDS": phases}), 600)

        # Execute the unchanged nested terminal helper with a controlled API
        # response and clock. A 575s call leaves 25s, rather than a fresh 600s.
        terminal = next(node for node in runtime.body if isinstance(node, ast.FunctionDef) and node.name == "terminal")
        command_id, clock, budgets = str(uuid4()), [100.0], []
        current = {"command_id": command_id, "running": False}
        ended = {"command_id": command_id, "status": "exited", "exit_code": 0}
        backend = SimpleNamespace(terminal=SimpleNamespace(
            handle=lambda role, frame: {"status": "completed"}, current=lambda: current),
            handoffs=SimpleNamespace(read_records=lambda kinds: [ended]))
        def call(function, seconds):
            budgets.append(("call", seconds))
            result = function()
            clock[0] += 575
            return result
        def wait(predicate, seconds):
            budgets.append(("completion", seconds))
            self.assertTrue(predicate())
        scope = {"time": SimpleNamespace(monotonic=lambda: clock[0]), "backend": backend,
                 "call": call, "wait": wait, "frame": lambda *args: {"fixture": True}}
        exec(compile(ast.Module(body=[deepcopy(terminal)], type_ignores=[]), str(harness.__file__), "exec"), scope)
        self.assertEqual(scope["terminal"]("owned timing fixture", 600)["ended"], ended)
        self.assertEqual(budgets, [("call", 600), ("completion", 25.0)])
