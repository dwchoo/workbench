"""Independent contract tests for the scoped Q01 gate; records here are fixtures."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCAL_OUTPUT = ROOT / ".workflow/dvl-local"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "tools"))
from validate_feature_gate import common, validate_q01  # noqa: E402
from summary_probe_support import Q01_ITEMS, canonical_digest  # noqa: E402


class IndependentGateContract(unittest.TestCase):
    def setUp(self) -> None:
        LOCAL_OUTPUT.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=LOCAL_OUTPUT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requirements = json.loads((ROOT / "docs/features/dev-validation-log-retention/requirements-r0.9-p5.json").read_text())
        self.plan = json.loads((ROOT / "docs/features/dev-validation-log-retention/PLAN.json").read_text())
        self.requirements_id = common.requirements(self.requirements)
        identity = {"files": {}, "index": [], "head": "original", "watch": [], "exclude": []}
        self.original = self.snapshot(identity)
        projection_identity = {
            "policy_version": 1, "policy_sha256": "a" * 64, "entries": {},
            "development_overlay": {"version": 1, "entries": {}},
            "omp": {"sha256": "b" * 64}, "route_identity": {"route": "fixture"},
            "host": {"os_family": "fixture"},
        }
        self.projection = {"schema_version": 1, "candidate_id": self.original["candidate"],
                           "identity": projection_identity, "digest": canonical_digest(projection_identity),
                           "unknowns": []}
        log = self.root / "observed.log"
        log.write_text("bounded fixture log\n")
        gates = {gate["id"]: gate for gate in self.requirements["gate_items"]}
        stamp = datetime.now(timezone.utc).isoformat()
        self.check = {
            "schema_version": 1, "check_id": "fixture-only-shape", "requirements_identity": self.requirements_id,
            "argv": ["fixture"], "cwd": str(self.root), "environment": {"fixture": "isolated"},
            "started_at": stamp, "finished_at": stamp, "exit_code": 0, "timed_out": False,
            "log": str(log), "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "before": self.original["candidate"], "after": self.original["candidate"], "stable": True,
            "items": [{"id": name, "acceptance_ids": gates[name]["acceptance_ids"],
                       "result": "passed", "observed_evidence_level": "runtime",
                       "evidence": "synthetic fixture; never runtime proof", "unknowns": []}
                      for name in Q01_ITEMS],
        }

    def snapshot(self, identity: dict) -> dict:
        return {"schema_version": 1, "candidate": common.digest(identity), "identity": identity,
                "unknowns": [], "stable": True, "workspace": str(self.root)}

    def judge(self, **changes) -> dict:
        args = {"requirements": self.requirements, "plan": self.plan, "checks": [self.check],
                "original_snapshot": self.original, "current_snapshot": self.original,
                "original_projection": self.projection, "current_projection": self.projection,
                "mode": "exact"}
        args.update(changes)
        return validate_q01(**args)

    def test_exact_and_dependency_revalidation_are_distinct(self) -> None:
        exact = self.judge()
        self.assertEqual(exact["kind"], "q01_exact_gate")
        self.assertEqual(set(exact["check_ids"]), set(Q01_ITEMS))
        self.assertFalse(exact["full_candidate_gate_pass"])
        current_identity = {**self.original["identity"], "head": "unrelated-ui-change"}
        current = self.snapshot(current_identity)
        with self.assertRaisesRegex(ValueError, "exact candidate changed"):
            self.judge(current_snapshot=current)
        scoped = self.judge(current_snapshot=current, mode="revalidate")
        self.assertEqual(scoped["original_candidate"], self.original["candidate"])
        self.assertEqual(scoped["current_candidate"], current["candidate"])
        self.assertEqual(scoped["kind"], "q01_scoped_revalidation")
        self.assertFalse(scoped["full_candidate_gate_pass"])

    def test_required_inventory_identity_and_record_fail_closed(self) -> None:
        mutations = []
        check = copy.deepcopy(self.check); check["items"] = check["items"][:-1]; mutations.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"][0]["id"] = "Q01-invented"; mutations.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"][0]["acceptance_ids"] = []; mutations.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"][0]["observed_evidence_level"] = "fixture"; mutations.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"][0]["unknowns"] = ["not_observed"]; mutations.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"][0]["result"] = "passed"; check["items"][0]["unknowns"] = ["missing"];
        mutations.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["requirements_identity"] = "0" * 64; mutations.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["before"] = "0" * 64; mutations.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["exit_code"] = 1; mutations.append({"checks": [check]})
        plan = copy.deepcopy(self.plan)
        plan["gate_items"] = [g for g in plan["gate_items"] if g["id"] != "Q01-route"]
        mutations.append({"plan": plan})
        plan = copy.deepcopy(self.plan); plan["requirements_identity"] = "0" * 64; mutations.append({"plan": plan})
        for mutation in mutations:
            with self.subTest(mutation=str(mutation)[:110]):
                with self.assertRaises(ValueError):
                    self.judge(**mutation)

    def test_forged_snapshot_projection_and_log_change_are_rejected(self) -> None:
        forged = copy.deepcopy(self.original); forged["candidate"] = "0" * 64
        with self.assertRaises(ValueError):
            self.judge(original_snapshot=forged)
        missing = copy.deepcopy(self.projection); missing["unknowns"] = ["missing required seam"]
        with self.assertRaisesRegex(ValueError, "projection"):
            self.judge(current_projection=missing)
        altered = copy.deepcopy(self.projection); altered["identity"]["entries"]["src/workbench/backend/service.py"] = {"kind": "absent"}
        altered["digest"] = canonical_digest(altered["identity"])
        with self.assertRaisesRegex(ValueError, "projection"):
            self.judge(current_projection=altered, mode="revalidate")
        Path(self.check["log"]).write_text("changed log\n")
        with self.assertRaisesRegex(ValueError, "log"):
            self.judge()


if __name__ == "__main__":
    unittest.main()
