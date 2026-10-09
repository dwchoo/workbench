"""Synthetic records are only negative fixtures for the Q01 judge."""
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
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "tools"))
from validate_feature_gate import common, validate_q01  # noqa: E402
from summary_probe_support import Q01_ITEMS, canonical_digest  # noqa: E402


class GateFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requirements = json.loads((ROOT / "docs/features/dev-validation-log-retention/requirements-r0.9-p5.json").read_text())
        self.plan = json.loads((ROOT / "docs/features/dev-validation-log-retention/PLAN.json").read_text())
        self.identity = common.requirements(self.requirements)
        self.candidate = common.digest({"files": {}, "index": [], "head": "fixture", "watch": [], "exclude": []})
        self.snapshot = {"schema_version": 1, "candidate": self.candidate,
                         "identity": {"files": {}, "index": [], "head": "fixture", "watch": [], "exclude": []},
                         "unknowns": [], "stable": True, "workspace": str(self.root)}
        projection_identity = {"policy_version": 1, "policy_sha256": "a" * 64,
                               "entries": {}, "development_overlay": {"version": 1, "entries": {}},
                               "omp": {"sha256": "b" * 64}, "route_identity": {"route": "fixture"},
                               "host": {"os_family": "fixture"}}
        self.projection = {"schema_version": 1, "candidate_id": self.candidate,
                           "identity": projection_identity,
                           "digest": canonical_digest(projection_identity), "unknowns": []}
        self.log = self.root / "check.log"
        self.log.write_text("synthetic fixture only\n")
        gates = {g["id"]: g for g in self.requirements["gate_items"]}
        now = datetime.now(timezone.utc).isoformat()
        self.check = {"schema_version": 1, "check_id": "synthetic-Q01",
                      "requirements_identity": self.identity, "argv": ["fixture"],
                      "cwd": str(self.root), "environment": {"fixture": True},
                      "started_at": now, "finished_at": now, "exit_code": 0,
                      "timed_out": False, "log": str(self.log),
                      "log_sha256": hashlib.sha256(self.log.read_bytes()).hexdigest(),
                      "before": self.candidate, "after": self.candidate, "stable": True,
                      "items": [{"id": name, "acceptance_ids": gates[name]["acceptance_ids"],
                                 "result": "passed", "evidence": "synthetic runtime-shaped record",
                                 "unknowns": [], "observed_evidence_level": "runtime"}
                                for name in Q01_ITEMS]}

    def judge(self, **changes) -> dict:
        args = {"requirements": self.requirements, "plan": self.plan,
                "checks": [self.check], "original_snapshot": self.snapshot,
                "current_snapshot": self.snapshot, "original_projection": self.projection,
                "current_projection": self.projection, "mode": "exact"}
        args.update(changes)
        return validate_q01(**args)

    def test_scoped_result_does_not_claim_full_gate(self) -> None:
        self.assertFalse(self.judge()["full_candidate_gate_pass"])

    def test_missing_stale_fixture_unknown_false_flags_and_log_tamper(self) -> None:
        changes = []
        check = copy.deepcopy(self.check); check["items"].pop(); changes.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["before"] = "0" * 64; changes.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"][0]["observed_evidence_level"] = "fixture"; changes.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"][0]["unknowns"] = ["unseen"] ; changes.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"][0]["result"] = "failed"; changes.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["exit_code"] = 1; changes.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["items"].append(check["items"][0]); changes.append({"checks": [check]})
        check = copy.deepcopy(self.check); check["requirements_identity"] = "0" * 64; changes.append({"checks": [check]})
        for mutation in changes:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    self.judge(**mutation)
        self.log.write_text("tampered\n")
        with self.assertRaisesRegex(ValueError, "log"):
            self.judge()

    def test_projection_and_current_candidate_invalidation(self) -> None:
        projection = copy.deepcopy(self.projection)
        projection["identity"]["entries"]["src/workbench/ipc/bridge_g3/mailbox.py"] = {"sha256": "0" * 64}
        projection["digest"] = canonical_digest(projection["identity"])
        with self.assertRaisesRegex(ValueError, "projection"):
            self.judge(current_projection=projection)
        current = copy.deepcopy(self.snapshot)
        current["identity"]["head"] = "other"
        current["candidate"] = common.digest(current["identity"])
        with self.assertRaisesRegex(ValueError, "exact candidate"):
            self.judge(current_snapshot=current)
        result = self.judge(current_snapshot=current, mode="revalidate")
        self.assertEqual(result["kind"], "q01_scoped_revalidation")
        self.assertFalse(result["full_candidate_gate_pass"])


if __name__ == "__main__":
    unittest.main()
