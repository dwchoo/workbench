"""Independent checks for one retained installed-OMP current Q01 batch.

No test here launches OMP. Mutated copies exercise the local collector/judge;
only the retained raw records establish runtime observations.
"""

from __future__ import annotations

import base64
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from tests.backend.q01_sdk_full_probe import current_probe


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from q01_recheck import REQUIRED_FACTS, judge_envelope  # noqa: E402

ITEMS = ("Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context",
         "Q01-priority", "Q01-recovery", "Q01-origin")
STAGES = ("prepare", "full", "contract", "context", "budget", "priority_input",
          "priority_runtime", "recovery", "origin")


def read(path: Path) -> dict | list:
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def select(trace: list[dict], event: str, case: str | None = None) -> list[dict]:
    return [row for row in trace if row.get("event") == event and
            (case is None or row.get("case") == case)]


class CurrentCollectorIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        supplied = os.environ.get("Q01_CURRENT_BATCH")
        output = os.environ.get("Q01_CURRENT_TEST_DIR")
        reread = os.environ.get("Q01_CURRENT_REJUDGE")
        if not supplied and not output and not reread:
            raise unittest.SkipTest(
                "Q01_CURRENT_BATCH, Q01_CURRENT_TEST_DIR and Q01_CURRENT_REJUDGE required"
                "; skipped tests are not a gate pass")
        if not supplied or not output:
            raise AssertionError("Q01_CURRENT_BATCH and Q01_CURRENT_TEST_DIR required")
        if not reread:
            raise AssertionError("Q01_CURRENT_REJUDGE required")
        cls.base = Path(supplied).resolve(strict=True)
        cls.output = Path(output).resolve(strict=True)
        cls.rejudge_path = Path(reread).resolve(strict=True)
        cls.rejudge = read(cls.rejudge_path)
        cls.index = read(cls.base / "stage-index.json")
        cls.observed = read(cls.base / "current-observations.json")
        cls.current_observed = read(Path(cls.rejudge["observations_path"]))
        cls.check = read(cls.base / "q01-check.json")
        cls.boundaries = read(cls.base / "capability-boundaries.json")
        cls.original_paths = {name: Path(cls.index["stages"][name]["artifact"])
                              for name in STAGES}
        cls.original_stages = {name: read(path) for name, path in cls.original_paths.items()}
        cls.paths = dict(cls.original_paths)
        cls.paths["priority_runtime"] = Path(cls.rejudge["new_priority_artifact"])
        cls.stages = copy.deepcopy(cls.original_stages)
        cls.stages["priority_runtime"] = read(cls.paths["priority_runtime"])
        binding = read(Path(cls.index["stages"]["contract"]["task_binding"]["artifact"]))
        task_binding = {
            "path": cls.index["stages"]["contract"]["task_binding"]["artifact"],
            "checks": binding["checks"], "bindings": binding["bindings"],
            "role_only": binding["role_only"]}
        cls.stages["contract"]["task_binding"] = task_binding
        cls.original_stages["contract"]["task_binding"] = copy.deepcopy(task_binding)
        cls.binding = binding

    def compose(self, stages: dict | None = None, errors: list[str] | None = None) -> dict:
        with tempfile.TemporaryDirectory(prefix="current-copy-", dir=self.output) as name:
            prep = {"isolated_root": name,
                    "prepared_readback": select(self.stages["prepare"]["trace"], "readback")[0]}
            return current_probe._compose(prep, copy.deepcopy(stages or self.stages),
                                          self.paths, list(errors or []))

    def test_exact_eight_frozen_rows_and_stage_custody(self) -> None:
        self.assertEqual(set(self.index["stages"]), set(STAGES))
        self.assertEqual(self.index["errors"], [])
        original_sha = self.index["current_probe_sha256"]
        snapshot_path = self.base / "snapshot.json"
        projection_path = self.base / "projection.json"
        snapshot = read(snapshot_path)
        self.assertEqual(snapshot["identity"]["files"]["tests/backend/q01_sdk_full_probe/current_probe.py"]["sha256"],
                         original_sha)
        self.assertEqual(self.rejudge["kind"], "offline_current_reader_mixed_stages")
        self.assertEqual(Path(self.rejudge["original_batch03"]), self.base)
        self.assertEqual(self.rejudge["original_stage_index_sha256"], digest(self.base / "stage-index.json"))
        self.assertEqual(self.rejudge["original_collector_sha256"], original_sha)
        self.assertEqual(digest(snapshot_path),
                         "0a6259ec0ed75a7c16326ac71280ea9b12daf5674bd49dfae316d56f1b7e1849")
        self.assertEqual(digest(projection_path),
                         "be7170ef617dcc3b37c4dae1bd2db40a90656d8e74b471e3ecec532aec407128")
        current_sha = digest(Path(current_probe.__file__))
        self.assertNotEqual(original_sha, current_sha)
        self.assertEqual(self.rejudge["current_collector_sha256"], current_sha)
        self.assertEqual(Path(self.rejudge["original_priority_artifact"]),
                         self.original_paths["priority_runtime"])
        self.assertEqual(self.rejudge["original_priority_artifact_sha256"],
                         digest(self.original_paths["priority_runtime"]))
        self.assertEqual(self.rejudge["new_priority_artifact_sha256"], digest(self.paths["priority_runtime"]))
        reread_observations = Path(self.rejudge["observations_path"])
        self.assertEqual(digest(reread_observations), self.rejudge["observations_sha256"])
        self.assertEqual(read(reread_observations), self.current_observed)
        self.assertEqual(self.rejudge["stage_artifacts_verified"], len(STAGES))
        self.assertEqual(self.rejudge["raw_http_body_count"], 48)
        self.assertEqual((self.rejudge["retained_raw_body_count"],
                          self.rejudge["new_priority_raw_body_count"]), (44, 4))
        for row in self.rejudge["raw_http_bodies"]:
            self.assertEqual(digest(Path(row["path"])), row["sha256"])
            self.assertEqual(Path(row["path"]).stat().st_size, row["bytes"])
        expected_source_issue = "origin:source_changed:" + str(ROOT / "tools/q01_recheck.py")
        self.assertEqual(self.rejudge["source_issues"], [expected_source_issue])
        self.assertEqual(self.rejudge["envelope_unknowns"], [expected_source_issue])
        self.assertEqual(self.rejudge["new_provider_posts_for_reread"], 0)
        self.assertEqual(self.rejudge["new_omp_launches_for_reread"], 0)
        self.assertEqual(set(self.rejudge["verdicts"]), set(ITEMS))
        for item in ITEMS:
            verdict = self.rejudge["verdicts"][item]
            self.assertEqual(verdict["result"], "inconclusive", item)
            self.assertIn(expected_source_issue, verdict["unknowns"], item)
            self.assertNotIn("evidence_ref", verdict)
            reconstructed = self.rejudge["reconstruction_verdicts"][item]
            self.assertEqual(reconstructed["result"], "passed", item)
            self.assertEqual(reconstructed["unknowns"], [], item)
            current = next(row for row in self.current_observed if row["id"] == item)
            self.assertEqual(reconstructed["evidence_ref"], current["evidence_ref"])
        priority_current = next(row for row in self.current_observed if row["id"] == "Q01-priority")
        self.assertIsNone(priority_current["facts"]["max_block_recorded"])
        self.assertEqual(priority_current["unknowns"], [])
        self.assertTrue(priority_current["facts"]["independent_delivery_observed"])
        self.assertGreater(priority_current["facts"]["nonshared_priority_measurement"]["max_enqueue_to_start_ms"], 0)
        self.assertEqual(len(self.observed), 8)
        self.assertEqual([row["id"] for row in self.observed], list(ITEMS))
        self.assertEqual([row["id"] for row in self.check["items"]], list(ITEMS))
        self.assertEqual(self.check["exit_code"], 0)
        self.assertEqual(self.check["before"], self.check["after"])
        self.assertEqual(digest(Path(self.check["log"])), self.check["log_sha256"])
        self.assertEqual(len(self.check["requirements_identity"]), 64)
        binary = Path(self.original_stages["prepare"]["argv"][0])
        self.assertEqual(digest(binary), self.original_stages["prepare"]["omp_binary_sha256"])
        self.assertEqual(self.rejudge["binary_sha256"], digest(binary))
        for name in STAGES:
            item = self.index["stages"][name]
            self.assertEqual(digest(self.original_paths[name]), item["sha256"], name)
            self.assertEqual(item["binary_sha256"], digest(binary), name)
            self.assertEqual(item["version"], "omp/18.8.6", name)
            source = self.original_stages[name].get("source_sha256", self.original_stages[name].get("source_sha"))
            self.assertTrue(source, name)
            for label, expected in source.items():
                p = Path(label)
                if not p.is_absolute():
                    directory = "q01_sdk_context_probe" if name == "context" else "q01_sdk_budget_probe"
                    p = ROOT / "tests/backend" / directory / label
                if name == "priority_runtime" and p.name == "run_priority_runtime.py":
                    self.assertEqual(expected, self.rejudge["original_priority_source_sha256"])
                elif name == "origin" and p == ROOT / "tools/q01_recheck.py":
                    self.assertEqual(snapshot["identity"]["files"]["tools/q01_recheck.py"]["sha256"], expected)
                    self.assertNotEqual(digest(p), expected)
                    self.assertEqual(current_probe._source_issues(name, self.original_stages[name]),
                                     [expected_source_issue])
                else:
                    self.assertEqual(digest(p), expected, (name, label))
        self.assertEqual(self.stages["priority_runtime"]["source_sha256"][
            str(ROOT / "tests/backend/q01_sdk_budget_probe/run_priority_runtime.py")],
            self.rejudge["new_priority_source_sha256"])
        self.assertEqual(digest(ROOT / "tests/backend/q01_sdk_budget_probe/run_priority_runtime.py"),
                         self.rejudge["new_priority_source_sha256"])
        for row in self.observed:
            self.assertEqual(tuple(name for name in row["facts"] if name in REQUIRED_FACTS[row["id"]]),
                             REQUIRED_FACTS[row["id"]])
            self.assertEqual(row["unknowns"], [])
            self.assertEqual(row["evidence_level"], "runtime")
            self.assertEqual(row["adapter"], "probe-local")
            self.assertEqual(Path(row["evidence_ref"]),
                             self.original_paths[{"Q01-route": "contract", "Q01-tools": "full",
                                         "Q01-skill": "contract", "Q01-result": "contract",
                                         "Q01-context": "context", "Q01-priority": "priority_runtime",
                                         "Q01-recovery": "recovery", "Q01-origin": "origin"}[row["id"]]]
                             if row["id"] != "Q01-result" else Path(self.binding_path))
        self.assertTrue(all(item["result"] == "passed" and item["unknowns"] == []
                            for item in self.check["items"]))
        self.assertTrue(all(p["stopped"] and p["pgid_absent"]
                            for p in self.boundaries["owned_processes"]))
        self.assertEqual(len(self.boundaries["owned_processes"]), 12)

    @property
    def binding_path(self) -> str:
        return self.index["stages"]["contract"]["task_binding"]["artifact"]

    def test_raw_48_posts_and_context_budget_priority_timeline(self) -> None:
        stages = self.original_stages
        raws: list[tuple[Path, str]] = []
        for name in ("full", "context", "budget", "priority_input", "recovery"):
            posts = stages[name].get("provider_posts", stages[name].get("posts", []))
            raws.extend((Path(p["raw_body_path"]), p.get("sha256", p.get("request_sha")))
                        for p in posts)
        priority = stages["priority_runtime"]
        raws.extend((Path(p["raw_body_path"]), p["sha256"])
                    for role in priority["providers"].values() for p in role["posts"])
        for name in ("contract", "origin"):
            actual = stages[name] if name == "contract" else read(Path(stages[name]["actual_runtime"]))
            private = self.original_paths[name].parent / ("private" if name == "contract" else "actual/private")
            posts = actual["provider_posts"]
            files = sorted(private.glob("raw-post-*.json"))
            self.assertEqual(len(files), len(posts))
            self.assertEqual(sorted(digest(p) for p in files), sorted(p["body_sha"] for p in posts))
            raws.extend((p, digest(p)) for p in files)
        self.assertEqual(len(raws), 48)
        for path, expected in raws:
            self.assertEqual(digest(path), expected, str(path))
            self.assertEqual(read(path)["model"], "scripted")
        context = stages["context"]
        parent = [p for p in context["posts"] if p["kind"] == "parent"]
        child = [p for p in context["posts"] if p["kind"] == "child"]
        self.assertEqual((len(parent), len(child)), (3, 6))
        receipts = select(context["trace"], "cycle_receipt_sent")
        self.assertEqual(len(receipts), 3)
        self.assertTrue(all(r["bytes"] <= 4096 for r in receipts))
        for index, post in enumerate(parent):
            raw = read(Path(post["raw_body_path"]))
            self.assertTrue({"read", "bash", "edit", "write"}.issubset(
                {t["function"]["name"] for t in raw["tools"]}))
            self.assertEqual(post["cycle_receipts"][index], 1)
            self.assertEqual(post["raw_markers"], [0, 0, 0])
        self.assertTrue(all(p["tools"] == ["db_summary_save"] for p in child))
        self.assertEqual(len(context["db_rows"]), 3)
        budget = stages["budget"]
        chosen = select(budget["trace"], "selection")[0]
        self.assertEqual((chosen["collectedBytes"], chosen["selectedBytes"], chosen["omittedBytes"]),
                         (1048576, 360, 1048216))
        preflight = select(budget["trace"], "dispatch_preflight")
        self.assertEqual(len(preflight), 2)
        self.assertTrue(all(p["admitted"] and p["contextBytes"] <= p["inputLimit"]
                            and p["outputReserve"] >= 8192 and p["fittedCap"] == 8192
                            for p in preflight))
        self.assertTrue(all(read(Path(p["raw_body_path"]))["max_completion_tokens"] == 8192
                            for p in budget["posts"]))
        self.assertLessEqual(budget["typed_contract"]["bytes"], 8192)
        wire = []
        for line in (self.original_paths["priority_runtime"].parent / "private/bridge-wire.jsonl").read_text().splitlines():
            record = json.loads(line)
            payload = base64.b64decode(record["raw_b64"], validate=True)
            self.assertEqual(sha256(payload).hexdigest(), record["sha256"])
            wire.append({**record, "frame": json.loads(payload)})
        self.assertEqual(len(wire), 41)
        release = priority["child_release_signal_ns"]
        self.assertLess(priority["child_hold_started_ns"], release)
        for role, kind in (("manager", "worker_terminal_done"), ("worker", "terminal_check")):
            parents = [p for p in priority["providers"][role]["posts"] if p["stage"] == "parent"]
            self.assertEqual(len(parents), 1)
            post = parents[0]
            offered = [w for w in wire if w["direction"] == "host_to_omp"
                       and w["frame"].get("notice", {}).get("type") == kind
                       and w["frame"]["notice"]["notice_id"] == post["notice_id"]]
            ack = [w for w in wire if w["direction"] == "omp_to_host"
                   and w["frame"].get("requestId") in {w["frame"]["requestId"] for w in offered}
                   and w["frame"].get("status") == "api_accepted"]
            request = [w for w in offered if ack and
                       w["frame"]["requestId"] == ack[0]["frame"]["requestId"]]
            self.assertEqual((len(request), len(ack)), (1, 1))
            self.assertLessEqual(request[0]["monotonic_ns"], ack[0]["monotonic_ns"])
            self.assertLessEqual(ack[0]["monotonic_ns"], post["received_ns"])
            self.assertLess(post["received_ns"], release)

    def test_task_grant_role_only_command_and_recovery_custody(self) -> None:
        contract = self.stages["contract"]
        request = contract["requests"]["positive"]
        typed = read(self.paths["contract"].parent / "private/typed-positive.json")
        binding = self.binding["bindings"]["positive"]
        private = self.paths["contract"].parent / "private"
        with sqlite3.connect(f"file:{private / 'backend.sqlite3'}?mode=ro", uri=True) as db:
            decisions = db.execute("""SELECT decision_id,revision,kind,created_at FROM task_decisions
                WHERE task_id=? ORDER BY created_at""", (binding["task_id"],)).fetchall()
            reservation = db.execute("""SELECT request_json,created_at FROM summary_requests_v1
                WHERE request_id=?""", (request["requestId"],)).fetchone()
            receipt = db.execute("SELECT request_id FROM summary_result_receipts_v1").fetchall()
            role_only = db.execute("SELECT request_id FROM summary_requests_v1 WHERE request_id=?",
                (self.binding["role_only"]["request"]["requestId"],)).fetchall()
        self.assertEqual([(row[0], row[1], row[2]) for row in decisions],
                         [(binding["scope_ref"], 1, "scope_approved"),
                          (binding["proceed_ref"], 1, "proceed")])
        self.assertLess(decisions[-1][3], reservation[1])
        self.assertEqual(json.loads(reservation[0]), request)
        self.assertEqual((request["authorizationRef"], request["revision"], request["subject"]["id"]),
                         (binding["proceed_ref"], 1, binding["task_id"]))
        self.assertEqual(request["sources"], [binding["selected_span"]])
        self.assertTrue(all(typed[key] == request[key] for key in
                            ("requestId", "inputKey", "skillDigest", "sources")))
        self.assertEqual(receipt, [(request["requestId"],)])
        self.assertEqual(role_only, [])
        self.assertEqual(self.binding["role_only"]["backend_decision"],
                         {"allowed": False, "reason": "no_matching_task_grant"})
        self.assertEqual(self.binding["role_only"]["confirmed_terminal_starts"], 0)
        self.assertIsNone(self.binding["role_only"]["backend_request_record"])
        self.assertFalse(any(p.get("case") == "user_role" for p in contract["provider_posts"]))
        origin = self.stages["origin"]
        actual = read(Path(origin["actual_runtime"]))
        cmd = origin["cases"]["positive"]
        cmd_request = actual["requests"]["positive"]
        important = Path(origin["actual_runtime"]).parent / "private/important" / (
            origin["important_db_uuid"] + ".sqlite3")
        with sqlite3.connect(f"file:{important}?mode=ro", uri=True) as db:
            start = db.execute("""SELECT source_id,event_key FROM subject_events_v1
                WHERE subject_kind='command' AND subject_id=? AND event_kind='terminal_started'""",
                (cmd["command_id"],)).fetchone()
            sources = [json.loads(row[0]) for row in db.execute("""SELECT sources_json FROM entries
                WHERE subject_kind='command' AND subject_id=?""", (cmd["command_id"],))]
        self.assertEqual((cmd_request["authorizationRef"], cmd_request["authorizationEpoch"]),
                         (start[0], int(start[1].split(":")[1])))
        self.assertEqual(cmd_request["sources"], [cmd["selected_confirmed_span"]])
        self.assertTrue(any(cmd["selected_confirmed_span"] in row for row in sources))
        self.assertEqual(read(Path(origin["actual_runtime"]).parent / "private/typed-positive.json")["sources"],
                         cmd_request["sources"])
        recovery = self.stages["recovery"]
        journal = recovery["journal"]
        self.assertEqual((journal["paused_gate"]["postCount"],
                          journal["load_failed_gate"]["postCount"]), (0, 0))
        self.assertEqual((journal["reopened_live_unknown"]["state"],
                          journal["reopened_killed_unknown"]["state"]), ("unknown", "unknown"))
        self.assertEqual(len(recovery["provider_posts"]), 5)
        self.assertEqual(len(journal["receipts"]), 2)
        self.assertEqual(journal["drops"], [[journal["receipts"][0][0], "duplicate"]])
        self.assertTrue(journal["replay_reservation_controls"]["exact_new_uuid"]["rejected"])

    def test_local_collector_reconstruction_and_corrupt_copies_fail_closed(self) -> None:
        baseline = self.compose()
        self.assertEqual(baseline["unknowns"], [])
        self.assertEqual(baseline["observations"], self.current_observed)
        baseline_verdict = judge_envelope(baseline, adapter="probe-local")
        self.assertEqual({item: row["result"] for item, row in baseline_verdict.items()},
                         {item: "passed" for item in ITEMS})

        changes = (
            ("request_grant_tampered", lambda x: x["contract"]["requests"]["positive"].update(
                authorizationRef="00000000-0000-0000-0000-000000000000"), "Q01-origin"),
            ("lost_grant", lambda x: x["contract"]["task_binding"]["bindings"]["positive"].update(
                proceed_ref="00000000-0000-0000-0000-000000000000"), "Q01-origin"),
            ("role_only_reserved", lambda x: x["contract"]["task_binding"]["role_only"].update(
                backend_request_record={"requestId": "unapproved"}), "Q01-origin"),
            ("lost_receipt", lambda x: x["full"]["trace"].__setitem__(slice(None), [e for e in x["full"]["trace"]
                if not (e.get("case") == "normal" and e.get("event") == "tool_end")]), "Q01-tools"),
            ("raw_parent", lambda x: x["context"]["posts"][2].update(raw_markers=[1, 0, 0]), "Q01-context"),
            ("due_notice_lost", lambda x: next(p for p in x["priority_runtime"]["providers"]["worker"]["posts"]
                if p["stage"] == "parent").update(notice_id="unmatched"), "Q01-priority"),
            ("late_typed_missing", lambda x: x["recovery"]["journal"].update(live_typed_exact=False),
                "Q01-recovery"),
            ("command_span_replaced", lambda x: x["origin"]["cases"]["positive"]["selected_confirmed_span"].update(
                digest="0" * 64), "Q01-origin"),
        )
        for name, change, item in changes:
            stages = copy.deepcopy(self.stages)
            change(stages)
            outcome = self.compose(stages)
            verdict = judge_envelope(outcome, adapter="probe-local")
            with self.subTest(name=name, item=item):
                if name == "due_notice_lost":
                    priority_row = next(row for row in outcome["observations"] if row["id"] == item)
                    self.assertFalse(priority_row["facts"]["due_check_priority"])
                self.assertNotEqual(verdict[item]["result"], "passed")
        with tempfile.TemporaryDirectory(prefix="current-raw-", dir=self.output) as folder:
            stages = copy.deepcopy(self.stages)
            original = stages["full"]["provider_posts"][0]
            tampered = Path(folder) / "post.json"
            tampered.write_bytes(Path(original["raw_body_path"]).read_bytes() + b" ")
            original["raw_body_path"] = str(tampered)
            outcome = self.compose(stages)
            self.assertTrue(any("raw_post_0_unbound" in e for e in outcome["unknowns"]))
            verdict = judge_envelope(outcome, adapter="probe-local")
            self.assertTrue(all(v["result"] != "passed" for v in verdict.values()))
        source = copy.deepcopy(self.stages["full"])
        name = next(iter(source["source_sha"]), None)
        self.assertIsNotNone(name)
        source["source_sha"][name] = "0" * 64
        errors = current_probe._source_issues("full", source)
        self.assertTrue(errors)
        outcome = self.compose(errors=errors)
        self.assertTrue(all(v["result"] != "passed" for v in
                            judge_envelope(outcome, adapter="probe-local").values()))
        stages = copy.deepcopy(self.stages)
        stages["full"]["omp_binary_sha"] = "0" * 64
        outcome = self.compose(stages)
        self.assertTrue(any("binary_or_version_changed" in e for e in outcome["unknowns"]))
        self.assertTrue(all(v["result"] != "passed" for v in
                            judge_envelope(outcome, adapter="probe-local").values()))


if __name__ == "__main__":
    unittest.main()
