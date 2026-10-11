"""Independent negative boundaries for the frozen Q01 projection and judge ports."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
LOCAL_OUTPUT = ROOT / ".workflow/dvl-local"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "tools"))
from q01_recheck import REQUIRED_FACTS, _load_port, judge_envelope, judge_observations, run  # noqa: E402
from summary_probe_support import (  # noqa: E402
    OVERLAY_FILES, POLICY_PATH, Q01_ITEMS, collect_projection, same_projection,
)


class IndependentProjectionContract(unittest.TestCase):
    def setUp(self) -> None:
        LOCAL_OUTPUT.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=LOCAL_OUTPUT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy_path = self.root / POLICY_PATH
        self.policy_path.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / POLICY_PATH, self.policy_path)
        self.policy = json.loads(self.policy_path.read_text())
        for path in (*self.policy["runtime_roots"], *self.policy["python_closure_roots"]):
            (self.root / path).mkdir(parents=True, exist_ok=True)
        for path in (*self.policy["runtime_files"], *self.policy["required_seams"], *OVERLAY_FILES):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n")
        self.omp = self.root / "fixture-omp"
        self.omp.write_text("#!/bin/sh\nprintf 'fixture-omp 1\\n'\n")
        self.omp.chmod(0o700)
        self.route = {"route": "existing-parent", "role": "manager",
                      "agent_definition_sha256": "a" * 64, "skill_sha256": "b" * 64,
                      "resolved_model": "scripted", "provider": "scripted",
                      "context_limit_tokens": 8192, "harness": "fixture",
                      "selection_epoch": "epoch-1", "source": "omp_readback"}

    def capture(self) -> dict:
        return collect_projection(self.root, omp_executable=self.omp,
                                  omp_bundle_identity="c" * 64, route_identity=self.route)

    def test_all_runtime_roots_and_import_closure_invalidate_on_change(self) -> None:
        original = self.capture()
        self.assertEqual(original["unknowns"], [])
        paths = (
            "src/workbench/backend/automation.py", "src/workbench/backend/service.py",
            "src/workbench/backend/flow_tasks.py", "src/workbench/backend/flow_terminal.py",
            "src/workbench/ipc/bridge_g3/mailbox.py", "src/workbench/contracts/new.json",
            "src/workbench/tasks/new.py", "src/workbench/storage/new.py",
            "src/workbench/workflow/new.py", "omp_bridge/g3/bridge.ts",
            "pyproject.toml", "tools/validate_feature_gate.py", "tools/q01_recheck.py",
        )
        for path in paths:
            target = self.root / path
            prior = target.read_bytes() if target.exists() else None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("changed = True\n")
            with self.subTest(path=path):
                self.assertFalse(same_projection(original, self.capture())[0])
            if prior is None:
                target.unlink()
            else:
                target.write_bytes(prior)

    def test_missing_mode_conditional_and_unknown_dynamic_imports(self) -> None:
        original = self.capture()
        seam = self.root / "src/workbench/backend/service.py"
        seam.unlink()
        with self.subTest(case="missing"):
            current = self.capture()
            self.assertTrue(current["unknowns"])
            self.assertFalse(same_projection(original, current)[0])
        seam.write_text("\n")
        seam.chmod(0o600)
        self.assertFalse(same_projection(original, self.capture())[0])
        seam.chmod(0o644)
        seam.write_text("if True:\n    from workbench.extra import value\n")
        extra = self.root / "src/workbench/extra.py"
        extra.write_text("value = 1\n")
        conditional = self.capture()
        self.assertIn("src/workbench/extra.py", conditional["identity"]["entries"])
        extra.write_text("value = 2\n")
        self.assertFalse(same_projection(conditional, self.capture())[0])
        seam.write_text("import importlib\nmodule_name = 'workbench.extra'\nimportlib.import_module(module_name)\n")
        dynamic = self.capture()
        self.assertTrue(any("unknown dynamic import" in issue for issue in dynamic["unknowns"]))
        self.assertFalse(same_projection(original, dynamic)[0])

    def test_policy_bytes_and_route_identity_are_part_of_projection(self) -> None:
        original = self.capture()
        self.policy_path.write_bytes(self.policy_path.read_bytes() + b" ")
        self.assertFalse(same_projection(original, self.capture())[0])
        self.policy_path.write_bytes((ROOT / POLICY_PATH).read_bytes())
        self.route["selection_epoch"] = "epoch-2"
        self.assertFalse(same_projection(original, self.capture())[0])
        self.route.pop("source")
        self.assertTrue(self.capture()["unknowns"])

    def test_aliased_dynamic_import_is_unknown_at_baseline(self) -> None:
        service = self.root / "src/workbench/backend/service.py"
        cases = (
            "from importlib import import_module\nname = 'workbench.extra'\nimport_module(name)\n",
            "import importlib as loader\nname = 'workbench.extra'\nloader.import_module(name)\n",
            "from importlib import import_module as load\nname = 'workbench.extra'\nload(name)\n",
            "import builtins as builtin_loader\nname = 'workbench.extra'\nbuiltin_loader.__import__(name)\n",
            "from importlib import import_module\nload = import_module\nname = 'workbench.extra'\nload(name)\n",
        )
        for source in cases:
            service.write_text(source)
            with self.subTest(source=source.splitlines()[0]):
                observed = self.capture()
                self.assertTrue(any("dynamic import" in issue for issue in observed["unknowns"]))

    def test_development_overlay_cannot_remove_canonical_core_roots(self) -> None:
        original = self.capture()
        self.policy_path.write_bytes(self.policy_path.read_bytes() + b" ")
        # Legal, record-only bytes still change the frozen policy identity.
        self.assertFalse(same_projection(original, self.capture())[0])
        for field, weakened_value in (
            ("runtime_roots", ["omp_bridge"]),
            ("required_seams", ["omp_bridge/g3/bridge.ts"]),
            ("python_closure_roots", ["src/workbench/tasks"]),
            ("workbench_owned_assets", ["omp_bridge/agents"]),
        ):
            weakened = copy.deepcopy(self.policy)
            weakened[field] = weakened_value
            self.policy_path.write_text(json.dumps(weakened))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.capture()
        weakened = copy.deepcopy(self.policy)
        weakened["include_python_package_initializers"] = False
        self.policy_path.write_text(json.dumps(weakened))
        with self.assertRaises(ValueError):
            self.capture()

    def test_python_package_initializer_presence_mode_and_bytes_invalidate(self) -> None:
        original = self.capture()
        for relative in ("src/workbench/__init__.py", "src/workbench/tasks/__init__.py"):
            target = self.root / relative
            self.assertEqual(original["identity"]["entries"][relative]["kind"], "absent")
            target.write_text("# package v1\n")
            with self.subTest(relative=relative, change="created"):
                self.assertFalse(same_projection(original, self.capture())[0])
            baseline = self.capture()
            target.write_text("# package v2\n")
            with self.subTest(relative=relative, change="bytes"):
                self.assertFalse(same_projection(baseline, self.capture())[0])
            target.write_text("# package v1\n")
            target.chmod(0o600)
            with self.subTest(relative=relative, change="mode"):
                self.assertFalse(same_projection(baseline, self.capture())[0])
            target.unlink()


class IndependentJudgePortContract(unittest.TestCase):
    def rows(self, adapter: str) -> list[dict]:
        source = "product_adapter" if adapter == "product" else "omp_event"
        rows = [{"id": name, "adapter": adapter, "source": source,
                 "evidence_level": "runtime", "evidence_ref": "fixture event reference",
                 "facts": {fact: True for fact in REQUIRED_FACTS[name]}, "unknowns": []}
                for name in Q01_ITEMS]
        priority = next(row for row in rows if row["id"] == "Q01-priority")
        from tests.backend.test_q01_priority_observations_independent import nonshared_facts
        priority["facts"] = nonshared_facts()
        return rows

    def test_all_eight_witnesses_and_product_source(self) -> None:
        rows = self.rows("product")
        self.assertEqual(set(judge_observations(rows, adapter="product")), set(Q01_ITEMS))
        self.assertTrue(all(v["result"] == "passed" for v in judge_observations(rows, adapter="product").values()))
        for mutation in (lambda r: r.pop(),
                         lambda r: r[0].update(source="omp_event"),
                         lambda r: r[0].update(evidence_level="fixture"),
                         lambda r: r[0].update(evidence_ref=""),
                         lambda r: r[0].update(unknowns=["unobserved"])):
            changed = copy.deepcopy(rows)
            mutation(changed)
            self.assertFalse(all(v["result"] == "passed" for v in judge_observations(changed, adapter="product").values()))

    def test_shared_priority_requires_measured_user_adoption(self) -> None:
        rows = self.rows("probe-local")
        priority = next(row for row in rows if row["id"] == "Q01-priority")
        priority["facts"].update(shared_session_or_lock=True, max_block_recorded=True, measured_block_ms=120)
        self.assertEqual(judge_observations(rows, adapter="probe-local")["Q01-priority"]["result"],
                         "blocked_adoption")
        priority["facts"].update(measured_block_ms=120, user_adoption_ref="decision-1")
        self.assertEqual(judge_observations(rows, adapter="probe-local")["Q01-priority"]["result"],
                         "passed")

    def test_priority_missing_sharing_measurement_or_independent_delivery_never_passes(self) -> None:
        base = self.rows("probe-local")
        cases = (
            lambda facts: facts.pop("shared_session_or_lock"),
            lambda facts: facts.update(shared_session_or_lock="false"),
            lambda facts: facts.pop("nonshared_priority_measurement"),
            lambda facts: facts.update(measured_block_ms=-1),
            lambda facts: facts.pop("independent_delivery_observed"),
            lambda facts: facts.update(independent_delivery_observed=False),
        )
        for mutate in cases:
            rows = copy.deepcopy(base)
            facts = next(row for row in rows if row["id"] == "Q01-priority")["facts"]
            mutate(facts)
            with self.subTest(facts=facts):
                self.assertNotEqual(judge_observations(rows, adapter="probe-local")["Q01-priority"]["result"],
                                    "passed")

    def test_envelope_unknown_applies_to_all_positive_rows_and_malformed_unknown_rejects(self) -> None:
        rows = self.rows("product")
        clean = judge_envelope({"observations": rows, "unknowns": []}, adapter="product")
        self.assertTrue(all(item["result"] == "passed" for item in clean.values()))
        unknown = judge_envelope({"observations": rows, "unknowns": ["collector_lost_event"]}, adapter="product")
        self.assertTrue(all(item["result"] == "inconclusive" for item in unknown.values()))
        self.assertTrue(all("collector_lost_event" in item["unknowns"] and "evidence_ref" not in item
                            for item in unknown.values()))
        for malformed in (None, "unknown", [""], [17]):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                judge_envelope({"observations": rows, "unknowns": malformed}, adapter="product")

    def test_guard_and_context_facts_are_individually_required(self) -> None:
        for item, fact in (("Q01-tools", "post_receipt_violation_rejected"),
                           ("Q01-tools", "upper_turn_unaffected"),
                           ("Q01-tools", "no_extra_model_turn"),
                           ("Q01-context", "output_ceiling_enforced"),
                           ("Q01-context", "full_output_reserved"),
                           ("Q01-origin", "work_source_ranges_bound")):
            rows = self.rows("probe-local")
            row = next(value for value in rows if value["id"] == item)
            row["facts"][fact] = False
            with self.subTest(item=item, fact=fact):
                self.assertEqual(judge_observations(rows, adapter="probe-local")[item]["result"], "failed")

    def test_product_port_missing_cannot_fallback_to_probe_local(self) -> None:
        with patch("q01_recheck.importlib.import_module", side_effect=ImportError("missing product adapter")) as load:
            self.assertIsNone(_load_port("product"))
            load.assert_called_once_with("workbench.backend.summary_compat_adapter")

    def test_prepared_adapter_is_closed_if_projection_or_collection_fails(self) -> None:
        requirements = json.loads((ROOT / "docs/features/dev-validation-log-retention/requirements-r0.9-p5.json").read_text())
        identity = {"route": "prepared"}
        prepared = {"route_identity": identity}
        snapshot = {"stable": True, "candidate": "a" * 64}
        paths = {name: LOCAL_OUTPUT / name for name in ("check.json", "log.json", "projection.json", "snapshot.json")}
        for failing_step in ("projection", "collection"):
            port = Mock()
            port.prepare_q01_context.return_value = prepared
            port.collect_q01_observations.side_effect = RuntimeError("collector failed")
            projection = Mock(side_effect=ValueError("projection failed")) if failing_step == "projection" else Mock(return_value={"identity": {}})
            with self.subTest(failing_step=failing_step):
                with (patch("q01_recheck._isolated_context"),
                      patch("q01_recheck.common.snapshot", return_value=snapshot),
                      patch("q01_recheck._load_port", return_value=port),
                      patch("q01_recheck.collect_projection", projection)):
                    with self.assertRaises((ValueError, RuntimeError)):
                        run(root=ROOT, requirements=requirements, adapter="product", context={},
                            omp_executable=ROOT / "unused-omp",
                            omp_bundle_identity="a" * 64, output=paths["check.json"], log=paths["log.json"],
                            projection_output=paths["projection.json"], snapshot_output=paths["snapshot.json"])
            port.close_q01_context.assert_called_once_with(prepared)
            if failing_step == "projection":
                port.collect_q01_observations.assert_not_called()


if __name__ == "__main__":
    unittest.main()
