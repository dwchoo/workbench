"""Harness fixtures only: Native process/bridge/OS ownership ports are controlled.

Launcher, environment construction, runtime readback adapter, metadata parser,
DEVNULL I/O selection and retained artifact writing execute unchanged. No Native/model
or network request is made; installed OMP capability remains a separate check.
"""

from contextlib import ExitStack, contextmanager, redirect_stderr
from copy import deepcopy
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.integration import live_model_metadata as probe
from workbench.ipc.bridge_g3.mailbox import BridgeDisconnected, BridgePeer


class ModelReadbackIndependent(unittest.TestCase):
    def setUp(self):
        output = os.environ.get("WB_DVL_PRECISE_TEST_OUTPUT")
        if output:
            self.assertTrue(Path(output).is_absolute() and Path(output).is_dir())
        self.parent = Path(tempfile.mkdtemp(prefix="metadata-fixture-", dir=output))
        if not output:
            self.addCleanup(shutil.rmtree, self.parent)
        self.table_path = probe._ROOT / "omp_bridge/omp-models.yml"
        self.table_bytes = self.table_path.read_bytes()
        table = probe.load_model_table(self.table_path)
        self.manager_selector = table["manager"]["models"]["default"]
        self.child_selector = table["worker"]["models"]["default"]
        self.table_digest = sha256(json.dumps(table, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":")).encode()).hexdigest()
        self.binary = self.parent / "fixture-omp"
        self.binary.write_bytes(b"controlled binary identity; never executed")
        self.roots, self.processes, self.bridges = [], [], []
        self.lifecycle = []
        self.native_marker = b"NATIVE-PRIVATE-PROMPT-AUTH-MARKER"
        self.instructions = "BUNDLED-INSTRUCTIONS-PRIVATE-MARKER"

    def metadata(self):
        def model(selector):
            provider, identifier = probe.selector_model(selector).split("/", 1)
            return {"provider": provider, "id": identifier, "api": "openai-responses",
                    "baseUrl": "https://example.invalid/v1", "contextWindow": 131072,
                    "maxTokens": 16384, "tokenizer": "cl100k_base", "apiKey": "DO-NOT-RETAIN"}

        return {"source": "parent_session_readback", "parentSessionId": "fixture-parent",
                "epoch": "fixture-stable-epoch", "model": model(self.manager_selector),
                "childSelection": {"source": "configured_worker_default", "selector": self.child_selector,
                    "modelTableDigest": self.table_digest, "model": model(self.child_selector),
                    "descriptorSource": "manager_model_registry_getAll_all",
                    "thinkingLevel": probe.selector_level(self.child_selector),
                    "serviceTierSource": "parent_session_readback"},
                "thinkingLevel": probe.selector_level(self.manager_selector), "serviceTierByFamily": {},
                "existingContextTokens": None, "outputCap": 8192, "outputLimitSource": "host_default",
                "outputLimitEnforced": None, "outputMeaning": "unknown", "reasoningTokens": None,
                "visibleResultTokens": None, "estimator": "utf8_bytes_upper_bound", "status": "inconclusive",
                "unknowns": ["current_context_unavailable", "output_ceiling_enforcement_unavailable",
                             "reasoning_budget_semantics_unavailable"]}

    @contextmanager
    def external_runtime(self, mode="normal"):
        owner = self
        real_mkdtemp, real_which = tempfile.mkdtemp, shutil.which

        def owned_root(*args, **kwargs):
            root = Path(real_mkdtemp(*args, **kwargs))
            owner.roots.append(root)
            owner.addCleanup(shutil.rmtree, root, True)
            return str(root)

        class NoInput(io.BytesIO):
            def write(self, data):
                owner.lifecycle.append("stdin_write")
                raise AssertionError("readback probe must never submit input")

            def close(self):
                if not self.closed:
                    owner.lifecycle.append("stdin_closed")
                super().close()

        class DiscardedOutput:
            def read(self, *args):
                owner.lifecycle.append("stdout_read")
                raise AssertionError("native output must not be read")

            def close(self):
                owner.lifecycle.append("stdout_close")
                raise AssertionError("native output must not be closed by a reader path")

        class Native:
            pid = 99_999_991  # Symbolic identity; all OS ownership ports below are controlled.

            def __init__(self, argv, **kwargs):
                self.argv, self.options = argv, kwargs
                self.stdin = NoInput()
                self.stdout = DiscardedOutput()
                self.returncode = 7 if mode == "early_exit" else None
                owner.processes.append(self)

            def poll(self):
                return self.returncode

        class Bridge:
            _thread = None

            def __init__(self, socket_path, tokens):
                self.socket_path = socket_path
                self.current = BridgePeer("manager", "fixture-parent", 4,
                                          Native.pid + (1 if mode == "peer_mismatch" else 0))
                self.requests, self.closed = [], False
                owner.bridges.append(self)

            def start(self):
                self.socket_path.touch(mode=0o600)

            def peer(self, role, **kwargs):
                if role == "worker":
                    raise BridgeDisconnected("no Worker was started")
                return self.current

            def request(self, role, request, **kwargs):
                owner.assertEqual((role, request["kind"]), ("manager", "summary_runtime"))
                self.requests.append((deepcopy(request), kwargs))
                if mode == "getter_unavailable":
                    return {"status": "deferred", "sent": False, "reason": "getter_unavailable"}
                metadata = owner.metadata()
                if mode == "readback_mismatch" and len(self.requests) == 2:
                    metadata["epoch"] = "different-epoch"
                if mode == "model_mismatch":
                    metadata["model"]["id"] = "other-configured-model"
                return {"status": "summary_runtime", "sent": False, "protected": False,
                        "sessionId": self.current.session_id, "generation": self.current.generation,
                        "runtime": {"metadata": metadata, "instructions": owner.instructions}}

            def close(self):
                self.closed = True
                self.socket_path.unlink(missing_ok=True)

        def version(argv, **kwargs):
            owner.assertEqual(argv, [str(owner.binary), "--version"])
            return subprocess.CompletedProcess(argv, 0, b"fixture-version\n")

        def stop(process, reference):
            owner.assertIs(process, owner.processes[-1])
            owner.assertEqual(reference, {"pid": Native.pid, "pgid": Native.pid, "start_ticks": "10101"})
            owner.lifecycle.append("owned_cleanup")
            if mode == "cleanup_failure":
                return "owned_process_identity_unavailable"
            process.returncode = process.returncode if process.returncode is not None else 0
            return None

        with ExitStack() as stack:
            stack.enter_context(patch.object(probe.tempfile, "mkdtemp", side_effect=owned_root))
            stack.enter_context(patch.object(probe.shutil, "which", side_effect=lambda name, *a, **kw:
                (None if mode == "missing" else str(self.binary)) if name == "omp" else real_which(name, *a, **kw)))
            stack.enter_context(patch.object(probe.subprocess, "run", side_effect=version))
            popen = stack.enter_context(patch.object(probe.subprocess, "Popen", side_effect=Native))
            stack.enter_context(patch.object(probe, "G3BridgeServer", Bridge))
            stack.enter_context(patch.object(probe.rt, "_identity", return_value="10101"))
            stack.enter_context(patch.object(probe.rt, "_stop_leader", side_effect=stop))
            stack.enter_context(patch.object(probe.rt, "_remaining", side_effect=lambda pgid:
                [Native.pid] if mode == "cleanup_failure" else []))
            yield popen

    def run_probe(self, mode):
        output = self.parent / mode
        with self.external_runtime(mode):
            code, result = probe.run_model_readback(output)
        self.assertEqual(json.loads((output / "result.json").read_bytes()), result)
        log = (output / "run.log.jsonl").read_bytes()
        self.assertLessEqual(len(log), 8192)
        self.assertLessEqual((output / "result.json").stat().st_size, 32768)
        self.assertEqual(sha256(log).hexdigest(), result["sanitized_log_sha256"])
        retained = log + (output / "result.json").read_bytes()
        for private in (self.native_marker, self.instructions.encode(), b"DO-NOT-RETAIN"):
            self.assertNotIn(private, retained)
        for process in self.processes:
            self.assertTrue(process.stdin.closed)
        self.assertFalse(result["native_output_captured"])
        self.assertIsNone(result["native_output_bytes"])
        self.assertIsNone(result["native_output_sha256"])
        self.assertTrue(all(bridge.closed for bridge in self.bridges))
        self.assertEqual(self.table_path.read_bytes(), self.table_bytes)
        return code, result

    def test_missing_omp_is_inconclusive_without_native_launch(self):
        code, result = self.run_probe("missing")
        self.assertEqual((code, result["status"], result["failure_reason"]), (2, "inconclusive", "omp_missing"))
        self.assertEqual(self.processes, [])
        self.assertEqual(result["cleanup"]["status"], "complete")
        self.assertTrue(all(not root.exists() for root in self.roots))

    def test_early_native_exit_is_inconclusive_and_closes_streams(self):
        code, result = self.run_probe("early_exit")
        self.assertEqual((code, result["status"], result["failure_reason"]),
                         (2, "inconclusive", "native_exited_before_manager_peer"))
        self.assertEqual(self.bridges[0].requests, [])
        self.assertEqual(result["cleanup"]["leader_exit_code"], 7)
        self.assertTrue(all(not root.exists() for root in self.roots))

    def test_getter_readback_model_and_peer_failures_cannot_pass(self):
        for mode in ("getter_unavailable", "readback_mismatch", "model_mismatch", "peer_mismatch"):
            with self.subTest(mode=mode):
                code, result = self.run_probe(mode)
                self.assertEqual((code, result["status"], result["capability_status"]),
                                 (2, "inconclusive", "inconclusive"))
                self.assertIsNotNone(result["failure_reason"])
                self.assertEqual(result["cleanup"]["status"], "complete")
                self.assertTrue(all(not root.exists() for root in self.roots))

    def test_cleanup_failure_cannot_turn_observed_getter_into_success(self):
        code, result = self.run_probe("cleanup_failure")
        self.assertEqual((code, result["status"], result["failure_reason"]),
                         (2, "inconclusive", "owned_cleanup_incomplete"))
        self.assertEqual(result["capability_status"], "observed")
        self.assertEqual(result["cleanup"]["status"], "incomplete")
        self.assertNotEqual(result["cleanup"]["remaining_owned"], [])
        self.assertTrue(self.roots[0].exists())

    def test_stable_bound_getter_uses_shipped_selection_and_preserves_soft_unknowns(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "HOST-CREDENTIAL", "ANTHROPIC_API_KEY": "HOST-CREDENTIAL",
                                     "PI_CONFIG_DIR": "/host-private-config", "HOME": "/host-private-home"}):
            code, result = self.run_probe("normal")
        self.assertEqual((code, result["status"], result["capability_status"]), (0, "readback_observed", "observed"))
        self.assertEqual((result["metadata_status"], result["overall_gate_status"]), ("inconclusive", "inconclusive"))
        self.assertEqual(result["metadata"]["epoch"], "fixture-stable-epoch")
        self.assertEqual(result["metadata"]["parentSessionId"], "fixture-parent")
        self.assertEqual(result["metadata"]["outputLimitSource"], "host_default")
        self.assertIsNone(result["metadata"]["existingContextTokens"])
        self.assertIsNone(result["metadata"]["outputLimitEnforced"])
        self.assertIsNone(result["metadata"]["reasoningTokens"])
        self.assertIsNone(result["metadata"]["visibleResultTokens"])
        self.assertEqual(result["metadata"]["outputMeaning"], "unknown")
        self.assertEqual(set(result["metadata"]["unknowns"]), {
            "current_context_unavailable", "output_ceiling_enforcement_unavailable",
            "reasoning_budget_semantics_unavailable"})
        self.assertEqual(result["configured_child_selector"], self.child_selector)
        self.assertEqual(result["model_table_file_sha256"], sha256(self.table_bytes).hexdigest())
        self.assertEqual(result["cleanup"]["status"], "complete")
        self.assertEqual([result[key] for key in ("prompt_submissions", "summary_requests",
                         "sdk_child_starts_requested", "paid_model_calls_requested")], [0, 0, 0, 0])
        process, bridge = self.processes[0], self.bridges[0]
        self.assertEqual(process.argv.count("--model"), 1)
        self.assertEqual(process.argv[process.argv.index("--model") + 1], probe.selector_model(self.manager_selector))
        env = process.options["env"]
        self.assertNotIn("HOST-CREDENTIAL", str(env))
        self.assertNotIn("/host-private", str(env))
        self.assertEqual(env["HOME"], str(self.roots[0] / "home"))
        self.assertTrue(process.options["start_new_session"])
        self.assertEqual(len(bridge.requests), 2)
        for request, kwargs in bridge.requests:
            self.assertEqual(request, {"kind": "summary_runtime", "expectedPeer": {"sessionId": "fixture-parent", "generation": 4}})
            self.assertEqual(kwargs["expected_peer"], ("fixture-parent", 4))
        self.assertTrue(all(not root.exists() for root in self.roots))

    def test_existing_output_is_not_overwritten_or_used_for_launch(self):
        output = self.parent / "existing"
        output.mkdir(mode=0o700)
        marker = output / "unrelated-marker"
        marker.write_bytes(b"preserve exact bytes")
        with self.external_runtime() as popen, redirect_stderr(io.StringIO()):
            code = probe.main(["--output", str(output)])
        self.assertEqual(code, 2)
        popen.assert_not_called()
        self.assertEqual(marker.read_bytes(), b"preserve exact bytes")
        self.assertEqual(list(output.iterdir()), [marker])
        self.assertEqual(self.roots, [])

    def test_devnull_output_and_unwritten_stdin_cleanup_need_no_drain(self):
        code, result = self.run_probe("normal")
        process = self.processes[0]
        self.assertEqual((process.options["stdout"], process.options["stderr"], process.options["stdin"]),
                         (subprocess.DEVNULL, subprocess.DEVNULL, subprocess.PIPE))
        self.assertEqual(self.lifecycle, ["owned_cleanup", "stdin_closed"])
        self.assertTrue(process.stdin.closed)
        self.assertEqual((code, result["status"], result["cleanup"]["status"]),
                         (0, "readback_observed", "complete"))
        self.assertNotIn("output_drain_complete", result["cleanup"])
        self.assertTrue(all(not root.exists() for root in self.roots))


if __name__ == "__main__":
    unittest.main()
