"""Owned fixture checks; these never supply Q01 runtime evidence."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from q01_recheck import judge_observations, nonshared_priority_issues
from tests.backend.q01_sdk_full_probe.current_probe import _priority_measurement, _rows


def priority_stage_fixture() -> dict:
    """Real stage shape with deterministic, synthetic monotonic timestamps."""
    stage = {"child_hold_started_ns": 1_000_000, "child_release_signal_ns": 9_000_000,
             "budget_trace": [{"event": "startup", "childSessionId": "child-fixture"}],
             "bridge": {"hello": {}, "requests": []}, "providers": {},
             "scheduler": {"tick60": {"status": "dispatched"}, "notice_dispatches": []},
             "timing": {"clock": "time.monotonic_ns", "parent_turn_events": []}}
    for role, kind, offset in (("worker", "terminal_check", 0),
                               ("manager", "worker_terminal_done", 1_000_000)):
        notice = role + "-notice"
        enqueue = 2_000_000 + offset
        ack = {"status": "api_accepted", "noticeId": notice, "requestId": role + "-request"}
        stage["bridge"]["hello"][role] = {"role": role, "ompSessionId": role + "-fixture"}
        stage["bridge"]["requests"].append({"role": role,
            "frame": {"kind": "notice", "notice": {"type": kind, "notice_id": notice}},
            "ack": ack, "sent_ns": enqueue + 1, "ack_observed_ns": enqueue + 2})
        stage["providers"][role] = {"posts": [{"stage": "parent", "notice_id": notice,
                                               "received_ns": enqueue + 4}]}
        stage["timing"]["parent_turn_events"].extend([
            {"role": role, "name": "agent_start", "observed_ns": enqueue + 3},
            {"role": role, "name": "agent_end", "observed_ns": enqueue + 5}])
        if role == "worker":
            stage["scheduler"]["notice_dispatches"].append({"notice_id": notice, "enqueue_ns": enqueue})
        else:
            stage["timing"]["end_accepted_enqueue_ns"] = enqueue
    return stage


def nonshared_facts() -> dict:
    return {"busy_deferred": True, "user_priority": True, "due_check_priority": True,
            "end_priority": True, "max_block_recorded": None, "measured_block_ms": None,
            "shared_session_or_lock": False, "independent_delivery_observed": True,
            "nonshared_priority_measurement": _priority_measurement(priority_stage_fixture())}


class PriorityObservationTests(unittest.TestCase):
    def verdict(self, facts: dict, unknowns: list | None = None) -> str:
        row = {"id": "Q01-priority", "adapter": "probe-local", "source": "omp_event",
               "evidence_level": "runtime", "evidence_ref": "synthetic fixture only",
               "facts": facts, "unknowns": unknowns or []}
        return judge_observations([row], adapter="probe-local")["Q01-priority"]["result"]

    def test_coherent_nonshared_windows_do_not_claim_mutex_measurement(self) -> None:
        facts = nonshared_facts()
        self.assertEqual(nonshared_priority_issues(facts["nonshared_priority_measurement"]), [])
        self.assertEqual(self.verdict(facts), "passed")
        rows = _rows({"Q01-priority": facts}, {item: Path("fixture") for item in (
            "Q01-route", "Q01-tools", "Q01-skill", "Q01-result", "Q01-context",
            "Q01-priority", "Q01-recovery", "Q01-origin")})
        self.assertEqual(rows[5]["unknowns"], [])
        self.assertIsNone(facts["measured_block_ms"])
        self.assertEqual(facts["nonshared_priority_measurement"]["internal_lock_wait"], "unavailable")

    def test_flags_or_null_block_alone_cannot_pass(self) -> None:
        facts = nonshared_facts()
        facts.pop("nonshared_priority_measurement")
        self.assertNotEqual(self.verdict(facts), "passed")
        facts["measured_block_ms"] = 0
        self.assertNotEqual(self.verdict(facts), "passed")

    def test_every_measurement_field_is_required(self) -> None:
        original = nonshared_facts()
        measure = original["nonshared_priority_measurement"]
        for field in measure:
            facts = copy.deepcopy(original)
            facts["nonshared_priority_measurement"].pop(field)
            with self.subTest(field=field):
                self.assertNotEqual(self.verdict(facts), "passed")
        for field in measure["notice_windows"][0]:
            facts = copy.deepcopy(original)
            facts["nonshared_priority_measurement"]["notice_windows"][0].pop(field)
            with self.subTest(window_field=field):
                self.assertNotEqual(self.verdict(facts), "passed")

    def test_false_identity_order_overlap_or_maximum_cannot_pass(self) -> None:
        original = nonshared_facts()
        mutations = (
            lambda m: m["session_ids"].update(child=m["session_ids"]["manager"]),
            lambda m: m["notice_windows"][0].update(session_id="wrong"),
            lambda m: m["notice_windows"][0].update(provider_notice_id="wrong"),
            lambda m: m["notice_windows"][0].update(ack_notice_id="wrong"),
            lambda m: m["notice_windows"][0].update(ack_request_id=m["notice_windows"][1]["ack_request_id"]),
            lambda m: m["notice_windows"][0].update(scheduler_notice_id="wrong"),
            lambda m: m["notice_windows"][0].update(ack_status="deferred"),
            lambda m: m["notice_windows"][0].update(role="manager"),
            lambda m: m["notice_windows"][0].update(agent_start_ns=False),
            lambda m: m["notice_windows"][0].update(agent_end_ns=1),
            lambda m: m.update(child_hold_interval_ns=[10_000_000, 20_000_000]),
            lambda m: m.update(clock="wall_clock"),
            lambda m: m.update(max_enqueue_to_start_ms=0),
            lambda m: m.update(max_enqueue_to_post_ms=float("nan")),
            lambda m: m["notice_windows"].pop(),
            lambda m: m.update(due_scheduler_status="waiting"),
        )
        for mutate in mutations:
            facts = copy.deepcopy(original)
            mutate(facts["nonshared_priority_measurement"])
            with self.subTest(mutate=mutate):
                self.assertNotEqual(self.verdict(facts), "passed")

    def test_existing_failures_and_unknowns_are_not_waived(self) -> None:
        for name in ("busy_deferred", "user_priority", "due_check_priority", "end_priority",
                     "independent_delivery_observed"):
            facts = nonshared_facts()
            facts[name] = False
            with self.subTest(name=name):
                self.assertEqual(self.verdict(facts), "failed")
        facts = nonshared_facts()
        facts["shared_session_or_lock"] = None
        self.assertNotEqual(self.verdict(facts), "passed")
        self.assertNotEqual(self.verdict(nonshared_facts(), ["timing_unobserved"]), "passed")

    def test_shared_route_still_requires_measured_block_and_adoption(self) -> None:
        facts = nonshared_facts()
        facts.update(shared_session_or_lock=True, max_block_recorded=True,
                     measured_block_ms=25, user_adoption_ref="adopted-fixture")
        self.assertEqual(self.verdict(facts), "passed")
        facts.pop("user_adoption_ref")
        self.assertEqual(self.verdict(facts), "blocked_adoption")
        facts["user_adoption_ref"] = "adopted-fixture"
        for measured in (None, False, -1, "25", float("nan")):
            facts["measured_block_ms"] = measured
            with self.subTest(measured=measured):
                self.assertNotEqual(self.verdict(facts), "passed")

    def test_raw_stage_attribution_mutations_are_reflected_in_judgment(self) -> None:
        mutations = (
            lambda s: s["bridge"]["hello"]["manager"].update(role="worker"),
            lambda s: s["providers"]["worker"]["posts"][0].update(notice_id="wrong"),
            lambda s: s["bridge"]["requests"][0]["ack"].update(noticeId="wrong"),
            lambda s: s["scheduler"]["notice_dispatches"][0].update(notice_id="wrong"),
            lambda s: s["timing"].update(clock="unknown"),
            lambda s: s["budget_trace"].append(s["budget_trace"][0]),
        )
        for mutate in mutations:
            stage = priority_stage_fixture()
            mutate(stage)
            facts = nonshared_facts()
            facts["nonshared_priority_measurement"] = _priority_measurement(stage)
            with self.subTest(mutate=mutate):
                self.assertNotEqual(self.verdict(facts), "passed")


if __name__ == "__main__":
    unittest.main()
