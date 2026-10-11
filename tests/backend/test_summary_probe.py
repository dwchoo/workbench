"""Fixture tests for the frozen Q01 projection and observation rules."""
from __future__ import annotations

import copy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from q01_recheck import REQUIRED_FACTS, _collect, judge_envelope, judge_observations, run  # noqa: E402
from tests.backend.test_q01_priority_observations import nonshared_facts
from summary_probe_support import (  # noqa: E402
    OVERLAY_FILES, POLICY_PATH, Q01_ITEMS, collect_projection, same_projection,
)


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        policy = self.root / POLICY_PATH
        policy.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / POLICY_PATH, policy)
        for directory in ("src/workbench/backend", "src/workbench/ipc",
                          "src/workbench/tasks", "src/workbench/storage",
                          "src/workbench/workflow", "omp_bridge"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        import json
        rules = json.loads(policy.read_text())
        for name in (*rules["required_seams"], *rules["runtime_files"], *OVERLAY_FILES):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n")
        self.omp = self.root / "fixture-omp"
        self.omp.write_text("#!/bin/sh\necho omp/fixture\n")
        self.omp.chmod(0o700)

    def capture(self) -> dict:
        route = {"route": "fixture-existing-parent", "role": "manager",
                 "agent_definition_sha256": "a" * 64, "skill_sha256": "b" * 64,
                 "resolved_model": "fixture", "provider": "scripted",
                 "context_limit_tokens": 8192, "harness": "fixture-harness",
                 "selection_epoch": "fixture-epoch", "source": "omp_readback"}
        return collect_projection(self.root, omp_executable=self.omp,
                                  omp_bundle_identity="a" * 64, route_identity=route)

    def test_directory_and_closure_changes_invalidate(self) -> None:
        original = self.capture()
        self.assertEqual(original["unknowns"], [])
        for path in ("src/workbench/backend/service.py", "src/workbench/ipc/bridge_g3/mailbox.py",
                     "src/workbench/tasks/new.py", "src/workbench/storage/metadata_sqlite/new.py",
                     "src/workbench/workflow/new.py", "tools/q01_recheck.py"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            previous = target.read_bytes() if target.exists() else None
            target.write_text("changed = True\n")
            with self.subTest(path=path):
                self.assertFalse(same_projection(original, self.capture())[0])
            if previous is None:
                target.unlink()
            else:
                target.write_bytes(previous)

    def test_dynamic_import_and_policy_change_invalidate(self) -> None:
        original = self.capture()
        (self.root / "src/workbench/backend/service.py").write_text(
            "import importlib\nname = 'workbench.tasks.repository'\nimportlib.import_module(name)\n")
        self.assertTrue(any("unknown dynamic import" in issue for issue in self.capture()["unknowns"]))
        (self.root / "src/workbench/backend/service.py").write_text("\n")
        policy = self.root / POLICY_PATH
        policy.write_bytes(policy.read_bytes() + b" ")
        self.assertFalse(same_projection(original, self.capture())[0])

    def test_static_import_closure_outside_required_roots(self) -> None:
        service = self.root / "src/workbench/backend/service.py"
        service.write_text("if True:\n    from workbench.extra import value\n")
        extra = self.root / "src/workbench/extra.py"
        extra.write_text("value = 1\n")
        original = self.capture()
        self.assertIn("src/workbench/extra.py", original["identity"]["entries"])
        extra.write_text("value = 2\n")
        self.assertFalse(same_projection(original, self.capture())[0])

    def test_weakened_canonical_policy_is_rejected_at_baseline(self) -> None:
        import json
        policy_file = self.root / POLICY_PATH
        policy = json.loads(policy_file.read_text())
        policy["runtime_roots"] = ["omp_bridge"]
        policy["required_seams"] = ["omp_bridge/g3/bridge.ts"]
        policy_file.write_text(json.dumps(policy))
        with self.assertRaisesRegex(ValueError, "canonical core minimum"):
            self.capture()

    def test_dynamic_import_aliases_are_unknown(self) -> None:
        service = self.root / "src/workbench/backend/service.py"
        cases = (
            "from importlib import import_module\nimport_module(name)\n",
            "from importlib import import_module as load\nload(name)\n",
            "import importlib as il\nil.import_module(name)\n",
            "import builtins as b\nb.__import__(name)\n",
            "from builtins import __import__ as load\nload(name)\n",
            "import importlib\nload = importlib.import_module\nload(name)\n",
        )
        for source in cases:
            service.write_text("name = 'workbench.extra'\n" + source)
            with self.subTest(source=source):
                self.assertTrue(any("dynamic import" in issue for issue in self.capture()["unknowns"]))

    def test_package_initializer_absence_new_file_and_bytes_are_in_closure(self) -> None:
        root_init = self.root / "src/workbench/__init__.py"
        original = self.capture()
        self.assertEqual(original["identity"]["entries"]["src/workbench/__init__.py"],
                         {"kind": "absent"})
        root_init.write_text("VALUE = 1\n")
        changed = self.capture()
        self.assertFalse(same_projection(original, changed)[0])
        root_init.write_text("VALUE = 2\n")
        self.assertFalse(same_projection(changed, self.capture())[0])


class ObservationTests(unittest.TestCase):
    def observations(self, adapter: str = "probe-local") -> list[dict]:
        source = "product_adapter" if adapter == "product" else "omp_event"
        rows = [{"id": name, "adapter": adapter, "source": source,
                 "evidence_level": "runtime", "evidence_ref": "fixture event reference",
                 "facts": {key: True for key in REQUIRED_FACTS[name]}, "unknowns": []}
                for name in Q01_ITEMS]
        rows[5]["facts"].update(nonshared_facts())
        return rows

    def test_all_required_witnesses_and_negative_mutations(self) -> None:
        observations = self.observations()
        self.assertTrue(all(row["result"] == "passed" for row in
                            judge_observations(observations, adapter="probe-local").values()))
        cases = [
            lambda rows: rows.pop(),
            lambda rows: rows[1]["facts"].update(receipt_request_bound=False),
            lambda rows: rows[1].update(evidence_level="fixture"),
            lambda rows: rows[1].update(unknowns=["guard_unverified"]),
            lambda rows: rows[1].update(source="model_self_report"),
        ]
        for mutate in cases:
            rows = copy.deepcopy(observations)
            mutate(rows)
            with self.subTest(mutate=mutate):
                self.assertFalse(all(v["result"] == "passed" for v in
                                     judge_observations(rows, adapter="probe-local").values()))

    def test_product_port_and_shared_priority_are_fail_closed(self) -> None:
        rows = self.observations("product")
        rows[5]["facts"].pop("shared_session_or_lock")
        self.assertNotEqual(judge_observations(rows, adapter="product")["Q01-priority"]["result"],
                            "passed")
        rows[5]["facts"].update(shared_session_or_lock=True, max_block_recorded=True, measured_block_ms=0)
        self.assertEqual(judge_observations(rows, adapter="product")["Q01-priority"]["result"],
                         "blocked_adoption")
        rows[5]["facts"].update(measured_block_ms=1200, user_adoption_ref="decision-ref")
        self.assertEqual(judge_observations(rows, adapter="product")["Q01-priority"]["result"],
                         "passed")
        rows[0]["source"] = "omp_event"
        self.assertNotEqual(judge_observations(rows, adapter="product")["Q01-route"]["result"],
                            "passed")
        with patch("q01_recheck.importlib.import_module", side_effect=ImportError("absent")):
            self.assertTrue(_collect("product", {}).get("unknowns"))

    def test_nonshared_priority_needs_observed_delivery_and_measurements(self) -> None:
        rows = self.observations()
        rows[5]["facts"].pop("independent_delivery_observed")
        self.assertNotEqual(judge_observations(rows, adapter="probe-local")["Q01-priority"]["result"],
                            "passed")
        rows[5]["facts"]["independent_delivery_observed"] = True
        rows[5]["facts"].pop("nonshared_priority_measurement")
        self.assertNotEqual(judge_observations(rows, adapter="probe-local")["Q01-priority"]["result"],
                            "passed")

    def test_adapter_unknowns_cannot_pass_positive_rows(self) -> None:
        rows = self.observations()
        verdicts = judge_envelope({"observations": rows,
                                   "unknowns": ["preflight_unverified"]}, adapter="probe-local")
        self.assertTrue(all(item["result"] != "passed" and
                            "preflight_unverified" in item["unknowns"] for item in verdicts.values()))
        for invalid in (None, "unknown", [None], [""]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "unknowns"):
                judge_envelope({"observations": rows, "unknowns": invalid}, adapter="probe-local")

    def test_prepared_context_closes_when_projection_or_validation_fails(self) -> None:
        class Port:
            def __init__(self, prepared):
                self.prepared = prepared
                self.closed = []

            def prepare_q01_context(self, context):
                return self.prepared

            def collect_q01_observations(self, prepared):
                raise ValueError("collection failed")

            def close_q01_context(self, prepared):
                self.closed.append(prepared)

        requirements = {"gate_items": [{"id": name} for name in Q01_ITEMS]}
        kwargs = dict(root=ROOT, requirements=requirements, adapter="probe-local", context={},
                      omp_executable=Path("/fixture/omp"), omp_bundle_identity="a" * 64,
                      output=Path("/fixture/check"), log=Path("/fixture/log"),
                      projection_output=Path("/fixture/projection"),
                      snapshot_output=Path("/fixture/snapshot"))
        for prepared, projection_error in (({"route_identity": {}}, True),
                                           ({"route_identity": {}}, False),
                                           ({"route_identity": "bad"}, False)):
            port = Port(prepared)
            with (patch("q01_recheck._isolated_context"),
                  patch("q01_recheck.common.requirements", return_value="fixture"),
                  patch("q01_recheck.common.snapshot", return_value={"candidate": "fixture"}),
                  patch("q01_recheck._load_port", return_value=port),
                  patch("q01_recheck.collect_projection", side_effect=(
                      ValueError("projection failed") if projection_error else None),
                        return_value={"identity": {}, "unknowns": []})):
                with self.subTest(prepared=prepared), self.assertRaises(ValueError):
                    run(**kwargs)
            self.assertEqual(port.closed, [prepared])

    def test_guard_negative_cases_and_post_receipt_event(self) -> None:
        for name in ("startup_missing_guard_prompt_sent_zero", "startup_noop_guard_prompt_sent_zero",
                     "startup_api_mismatch_prompt_sent_zero", "startup_stale_receipt_prompt_sent_zero",
                     "runtime_policy_mismatch_sent_zero",
                     "post_receipt_violation_rejected", "upper_turn_unaffected",
                     "normal_permissions_restored"):
            rows = self.observations()
            rows[1]["facts"][name] = False
            with self.subTest(name=name):
                self.assertEqual(judge_observations(rows, adapter="probe-local")["Q01-tools"]["result"],
                                 "failed")


if __name__ == "__main__":
    unittest.main()
