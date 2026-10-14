"""LG-D30/LG-D28 owned setup fixtures, not Native/operating MODEL evidence."""
import ast
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workbench.backend import summary_compat_runtime as runtime
from workbench.backend import summary_compat_priority as priority
from workbench.backend.launcher import load_model_table, omp_environment
from workbench.backend.summary_adapter import prepare_summary
from workbench.contracts.logs_v1 import canonical_json

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ.get("WB_DVL_OWNED_TEST_OUTPUT", tempfile.gettempdir()))


class OwnedSetupIndependentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="owned-python-", dir=OUTPUT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for boundary in ("subprocess.Popen", "socket.socket", "socket.create_connection"):
            tripwire = patch(boundary, side_effect=AssertionError("Native/network launch forbidden"))
            tripwire.start()
            self.addCleanup(tripwire.stop)

    def test_private_table_full_digest_role_env_and_catalog_do_not_change_shipped_bytes(self):
        shipped = ROOT / "omp_bridge/omp-models.yml"
        before_bytes = shipped.read_bytes()
        before = load_model_table(shipped)
        table = runtime._owned_model_table(ROOT)
        expected = deepcopy(before)
        expected["worker"]["models"]["default"] = "ownedverify/worker:high"
        self.assertEqual(table, expected)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-ambient-secret", "HTTPS_PROXY": "fake-proxy",
                                     "LD_PRELOAD": "fake-preload", "WORKBENCH_G3_SUMMARY_SELECTION": "ambient"}):
            env = runtime._environment(self.root)
        for name in ("OPENAI_API_KEY", "HTTPS_PROXY", "LD_PRELOAD", "WORKBENCH_G3_SUMMARY_SELECTION"):
            self.assertNotIn(name, env)
        self.assertEqual(env["HOME"], str(self.root / "home"))
        plan = SimpleNamespace(omp_version="fixture-no-native")
        manager = omp_environment(env, plan, role="manager", token="controlled-token",
                                  bridge_socket=self.root / "absent.sock", models=table)
        selection = json.loads(manager["WORKBENCH_G3_SUMMARY_SELECTION"])
        self.assertEqual(selection["selector"], "ownedverify/worker:high")
        self.assertEqual(selection["source"], "configured_worker_default")
        self.assertEqual(selection["modelTableDigest"], sha256(canonical_json(table)).hexdigest())
        worker = omp_environment(env, plan, role="worker", token="controlled-token",
                                 bridge_socket=self.root / "absent.sock", models=table)
        self.assertNotIn("WORKBENCH_G3_SUMMARY_SELECTION", worker)
        (self.root / "agent").mkdir(mode=0o700)
        endpoint = "http://127.0.0.1:19423/v1"
        runtime._write_owned_models(self.root, endpoint, tokenizer="claude-v3")
        text = (self.root / "agent/models.yml").read_text()
        self.assertIn("auth: none", text)
        self.assertIn("baseUrl: " + endpoint, text)
        blocks = re.findall(r"      - id: (\w+)\n(.*?)(?=      - id:|\Z)", text, re.S)
        self.assertEqual([name for name, _ in blocks], ["scripted", "worker"])
        for name, block in blocks:
            with self.subTest(model=name):
                self.assertIn("contextWindow: 131072", block)
                self.assertIn("maxTokens: 8192", block)
                self.assertIn("reasoning: true", block)
                self.assertIn("tokenizer: claude-v3", block)
        self.assertEqual((self.root / "agent/models.yml").stat().st_mode & 0o777, 0o600)
        table["worker"]["models"]["default"] = "fixture/in-memory-change:low"
        self.assertEqual(shipped.read_bytes(), before_bytes)
        self.assertEqual(load_model_table(shipped), before)

    def test_factory_environment_preserves_private_native_catalog_and_config_paths(self):
        # Evaluate unmodified actual factory expressions, including home, through
        # the real launcher. Root corrected the product callsites after test01.
        (self.root / "agent").mkdir(mode=0o700)
        (self.root / "config").mkdir(mode=0o700)
        xdg = [self.root / ("xdg-" + name) for name in ("config", "data", "state", "cache")]
        for directory in xdg:
            directory.mkdir(mode=0o700)
            self.assertEqual(list(directory.iterdir()), [])
        runtime._write_owned_models(self.root, "http://127.0.0.1:19423/v1")
        plan = SimpleNamespace(omp_version="fixture-no-native")
        for module, role in ((runtime, "manager"), (priority, "manager"), (priority, "worker")):
            with self.subTest(factory=module.__name__, role=role):
                source = Path(module.__file__)
                calls = [node for node in ast.walk(ast.parse(source.read_text()))
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "omp_environment"]
                self.assertEqual(len(calls), 1, "single actual owned factory callsite")
                bindings = {"root": self.root, "runtime": ROOT, "plan": plan, "role": role,
                            "tokens": {"manager": "controlled-manager", "worker": "controlled-worker"},
                            "socket_path": self.root / "absent.sock"}
                actual = eval(compile(ast.Expression(calls[0]), str(source), "eval"), module.__dict__, bindings)
                self.assertEqual((actual.get("PI_CODING_AGENT_DIR"), actual.get("PI_CONFIG_DIR")),
                                 (str(self.root / "agent"), str(self.root / "config")),
                                 "owned launcher must retain private catalog/config via its home contract")
                self.assertTrue((Path(actual["PI_CODING_AGENT_DIR"]) / "models.yml").is_file())
                for name, directory in zip(("CONFIG", "DATA", "STATE", "CACHE"), xdg):
                    self.assertEqual(actual["XDG_" + name + "_HOME"], str(directory))
                    self.assertEqual(list(directory.iterdir()), [])
                if role == "worker":
                    self.assertNotIn("WORKBENCH_G3_SUMMARY_SELECTION", actual)

    def test_actual_three_range_producer_collector_prepare_preserves_child_count_and_coverage(self):
        # Match the owned factory's pre-existing private data root.
        (self.root / "data").mkdir(mode=0o700)
        facts = {"raw_queries": []}
        _, _, _, _, request, pieces, originals = priority.producer(self.root, facts)
        self.assertEqual(facts["producer"]["bytes"], 12288)
        self.assertEqual(facts["producer"]["collector_pieces"], 3)
        self.assertEqual(facts["producer"]["capture_failures"], [])
        self.assertEqual([(r["start"], r["end"]) for r in originals], [(0, 4096), (4096, 8192), (8192, 12288)])
        self.assertEqual([len(p.text.encode()) for p in pieces], [4096, 4096, 4096])
        self.assertEqual(len(facts["raw_queries"]), 3)
        model = {"provider": "ownedverify", "id": "scripted", "api": "openai-completions",
                 "baseUrl": "http://127.0.0.1:19423/v1", "contextWindow": 131072,
                 "maxTokens": 8192, "tokenizer": "claude-v3"}
        child = {**model, "id": "worker"}
        metadata = {"source": "parent_session_readback", "parentSessionId": "controlled-parent",
                    "epoch": "controlled-epoch", "model": model, "thinkingLevel": "high",
                    "serviceTierByFamily": {}, "childSelection": {
                        "source": "configured_worker_default", "selector": "ownedverify/worker:high",
                        "modelTableDigest": "a" * 64, "model": child, "thinkingLevel": "high",
                        "descriptorSource": "manager_model_registry_getAll_all", "serviceTierSource": "parent_session_readback"},
                    "status": "inconclusive", "unknowns": ["current_context_unavailable",
                        "output_ceiling_enforcement_unavailable", "reasoning_budget_semantics_unavailable"],
                    "existingContextTokens": None, "outputCap": 8192, "outputLimitSource": "host_default",
                    "outputLimitEnforced": None, "reasoningTokens": None, "visibleResultTokens": None,
                    "outputMeaning": "unknown", "estimator": "utf8_bytes_upper_bound"}
        original_metadata = deepcopy(metadata)
        queries = []
        def count(query):
            queries.append(deepcopy(query))
            return {"version": query["version"], "ownerDigest": query["ownerDigest"],
                    "fragmentDigests": query["fragmentDigests"], "encoding": "ClaudeV3", "exact": True,
                    "estimator": "native_known_family_strict",
                    "tokens": [max(1, len(fragment.encode()) // 4) for fragment in query["fragments"]]}
        prepared = prepare_summary(request, pieces, metadata=metadata,
                                   instructions="Controlled bundled DB-only task.", count_fragments=count)
        selected = [span for item in prepared["selected"] for span in item.get("ranges", [item.get("range")])]
        omitted = [item["range"] for item in prepared["omitted"]]
        self.assertGreaterEqual(len(selected), 1)
        self.assertEqual(Counter(canonical_json(r) for r in selected + omitted),
                         Counter(canonical_json(r) for r in originals))
        self.assertEqual(len(set(canonical_json(r) for r in selected + omitted)), 3)
        for query in queries:
            self.assertEqual(query["owner"]["model"], child)
            self.assertEqual(query["owner"]["parentSessionId"], "controlled-parent")
        self.assertTrue(queries)
        self.assertEqual(prepared["countBinding"]["ownerDigest"], queries[-1]["ownerDigest"])
        self.assertEqual(prepared["countBinding"]["encoding"], "ClaudeV3")
        self.assertEqual(prepared["budget"]["outputReserve"], 8192)
        self.assertEqual(prepared["budget"]["inputLimit"], 131072)
        self.assertLessEqual(prepared["budget"]["mandatoryWireBytes"], 32768)
        self.assertEqual(prepared["metadata"], original_metadata)
        self.assertEqual(metadata, original_metadata)

    def test_actual_provider_handler_accepts_worker_db_only_and_refuses_parent_or_other_tool(self):
        system = "Controlled fixed system prompt"
        (self.root / "expected.json").write_text(json.dumps({"requestId": "fixture-request", "systemSha": sha256(system.encode()).hexdigest()}))
        for model, tool, expected_status in (("worker", "db_summary_save", 200),
                                              ("scripted", "db_summary_save", 400), ("worker", "bash", 400)):
            with self.subTest(model=model, tool=tool):
                posts, violations, responses = [], [], []
                handler_type = runtime._handler(self.root, posts, violations)
                handler = handler_type.__new__(handler_type)
                raw = json.dumps({"model": model, "tools": [{"function": {"name": tool}}],
                                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": "fixed input"}]}).encode()
                handler.headers = {"Content-Length": str(len(raw))}
                handler.path, handler.rfile, handler.wfile = "/v1/chat/completions", BytesIO(raw), BytesIO()
                handler.send_response = lambda status: responses.append(status)
                handler.send_header = lambda *_args: None
                handler.end_headers = lambda: None
                handler.send_error = lambda status: responses.append(status)
                handler.do_POST()
                self.assertEqual(responses, [expected_status])
                if expected_status == 200:
                    self.assertEqual(violations, [])
                    frames = [json.loads(line[6:]) for line in handler.wfile.getvalue().decode().splitlines()
                              if line.startswith("data: ") and line != "data: [DONE]"]
                    self.assertTrue(frames)
                    self.assertEqual({f["model"] for f in frames}, {"worker"})
                else:
                    self.assertTrue(violations)
                    self.assertEqual(handler.wfile.getvalue(), b"")
        parent_bytes, _ = priority._parent_payload("controlled parent notice")
        parent_frames = [json.loads(line[6:]) for line in parent_bytes.decode().splitlines()
                         if line.startswith("data: ") and line != "data: [DONE]"]
        self.assertEqual({f["model"] for f in parent_frames}, {"scripted"})


if __name__ == "__main__":
    unittest.main()
