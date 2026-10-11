"""SPEC priority behavior tests. Synthetic rows prove judge behavior, not runtime."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from q01_recheck import REQUIRED_FACTS, judge_envelope  # noqa: E402


def measurement_fixture() -> dict:
    """Two independently attributable deliveries during one held child turn."""
    sessions = {"manager": "session-manager", "worker": "session-worker", "child": "session-child"}
    windows = []
    for role, kind, offset in (("manager", "worker_terminal_done", 0), ("worker", "terminal_check", 10000000)):
        enqueue = 1010000000 + offset
        notice = "notice-" + role
        windows.append({"role": role, "kind": kind, "session_id": sessions[role],
                        "notice_id": notice, "ack_notice_id": notice, "provider_notice_id": notice,
                        "ack_request_id": "request-" + role, "ack_status": "api_accepted",
                        "provider_stage": "parent", "scheduler_notice_id": notice if role == "worker" else None,
                        "enqueue_ns": enqueue, "frame_request_ns": enqueue + 1000000,
                        "ack_observed_ns": enqueue + 2000000, "agent_start_ns": enqueue + 3000000,
                        "provider_post_ns": enqueue + 4000000, "agent_end_ns": enqueue + 5000000,
                        "enqueue_to_ack_ms": 2.0, "enqueue_to_start_ms": 3.0,
                        "enqueue_to_post_ms": 4.0, "turn_hold_overlap_ms": 2.0})
    return {"kind": "delivery_turn_overlap", "clock": "time.monotonic_ns", "session_ids": sessions,
            "child_hold_interval_ns": [1000000000, 1100000000], "notice_windows": windows,
            "due_scheduler_status": "dispatched", "internal_lock_wait": "unavailable",
            "max_enqueue_to_start_ms": 3.0, "max_enqueue_to_post_ms": 4.0}


def nonshared_facts() -> dict:
    return {**{name: True for name in REQUIRED_FACTS["Q01-priority"]},
            "max_block_recorded": None, "measured_block_ms": None, "shared_session_or_lock": False,
            "independent_delivery_observed": True, "nonshared_priority_measurement": measurement_fixture()}


def rows_fixture() -> list[dict]:
    return [{"id": item, "adapter": "probe-local", "source": "omp_event", "evidence_level": "runtime",
             "evidence_ref": "synthetic-attributed-event", "unknowns": [],
             "facts": nonshared_facts() if item == "Q01-priority" else
                      {fact: True for fact in REQUIRED_FACTS[item]}} for item in REQUIRED_FACTS]


class PriorityObservationIndependentTests(unittest.TestCase):
    def verdict(self, facts: dict) -> dict:
        rows = rows_fixture()
        next(row for row in rows if row["id"] == "Q01-priority")["facts"] = facts
        verdicts = judge_envelope({"observations": rows, "unknowns": []}, adapter="probe-local")
        self.assertTrue(all(row["result"] == "passed" for item, row in verdicts.items() if item != "Q01-priority"))
        return verdicts["Q01-priority"]

    def reject_measurement(self, mutations) -> None:
        for label, mutation in mutations:
            facts = nonshared_facts()
            mutation(facts["nonshared_priority_measurement"])
            with self.subTest(case=label):
                self.assertNotEqual(self.verdict(facts)["result"], "passed")

    def test_complete_nonshared_delivery_passes_with_mutex_limitation(self) -> None:
        facts = nonshared_facts()
        self.assertEqual(self.verdict(facts)["result"], "passed")
        self.assertIsNone(facts["measured_block_ms"])
        self.assertIsNone(facts["max_block_recorded"])
        self.assertEqual(facts["nonshared_priority_measurement"]["internal_lock_wait"], "unavailable")

    def test_flag_only_or_missing_measurement_never_passes(self) -> None:
        for value in (None, {}, False, [], "observed"):
            facts = nonshared_facts()
            facts["nonshared_priority_measurement"] = value
            with self.subTest(value=value):
                self.assertNotEqual(self.verdict(facts)["result"], "passed")
        facts = nonshared_facts()
        facts.pop("nonshared_priority_measurement")
        self.assertNotEqual(self.verdict(facts)["result"], "passed")

    def test_distinct_nonempty_session_identity_required(self) -> None:
        self.reject_measurement([
            ("duplicate", lambda m: m["session_ids"].update(child=m["session_ids"]["manager"])),
            ("empty", lambda m: m["session_ids"].update(manager=" ")),
            ("missing", lambda m: m["session_ids"].pop("worker")),
            ("wrong_type", lambda m: m.update(session_ids=[])),
            ("wrong_bound_session", lambda m: m["notice_windows"][0].update(session_id="session-worker")),
        ])

    def test_notice_and_request_attribution_required(self) -> None:
        mutations = [(field, lambda m, field=field: m["notice_windows"][0].update({field: "wrong"}))
                     for field in ("ack_notice_id", "provider_notice_id", "ack_status", "provider_stage")]
        mutations += [("scheduler", lambda m: m["notice_windows"][1].update(scheduler_notice_id="wrong")),
                      ("duplicate_notice", lambda m: m["notice_windows"][1].update(notice_id=m["notice_windows"][0]["notice_id"])),
                      ("duplicate_request", lambda m: m["notice_windows"][1].update(ack_request_id=m["notice_windows"][0]["ack_request_id"])),
                      ("empty_request", lambda m: m["notice_windows"][0].update(ack_request_id=""))]
        self.reject_measurement(mutations)

    def test_two_unique_role_windows_required(self) -> None:
        self.reject_measurement([
            ("missing_due", lambda m: m["notice_windows"].pop()),
            ("missing_end", lambda m: m["notice_windows"].pop(0)),
            ("repeated_role", lambda m: m["notice_windows"].__setitem__(1, copy.deepcopy(m["notice_windows"][0]))),
            ("wrong_kind", lambda m: m["notice_windows"][1].update(kind="worker_terminal_done")),
            ("malformed_window", lambda m: m["notice_windows"].__setitem__(0, None)),
        ])

    def test_monotonic_order_and_numeric_time_types_required(self) -> None:
        mutations = []
        for field in ("enqueue_ns", "frame_request_ns", "ack_observed_ns", "agent_start_ns", "provider_post_ns", "agent_end_ns"):
            for value in (None, True, "1010000000", -1, 0, 1010000000.0):
                mutations.append((f"{field}:{value!r}", lambda m, field=field, value=value:
                                  m["notice_windows"][0].update({field: value})))
        mutations.append(("reversed_ack", lambda m: m["notice_windows"][0].update(ack_observed_ns=1016000000)))
        self.reject_measurement(mutations)

    def test_positive_child_hold_overlap_and_delivery_before_release_required(self) -> None:
        self.reject_measurement([
            ("reversed_hold", lambda m: m.update(child_hold_interval_ns=[1100000000, 1000000000])),
            ("bool_hold", lambda m: m.update(child_hold_interval_ns=[True, 1100000000])),
            ("release_before_post", lambda m: m.update(child_hold_interval_ns=[1000000000, 1014000000])),
            ("hold_after_delivery", lambda m: m.update(child_hold_interval_ns=[1050000000, 1100000000])),
            ("zero_overlap", lambda m: m["notice_windows"][0].update(agent_end_ns=1013000000)),
        ])

    def test_actual_derived_delays_and_maxima_required(self) -> None:
        mutations = []
        for field in ("enqueue_to_ack_ms", "enqueue_to_start_ms", "enqueue_to_post_ms", "turn_hold_overlap_ms"):
            for value in (None, True, "4", float("nan"), float("inf"), -1, 999):
                mutations.append((f"{field}:{value!r}", lambda m, field=field, value=value:
                                  m["notice_windows"][0].update({field: value})))
        for field in ("max_enqueue_to_start_ms", "max_enqueue_to_post_ms"):
            for value in (None, True, "4", float("nan"), float("inf"), -1, 999):
                mutations.append((f"{field}:{value!r}", lambda m, field=field, value=value: m.update({field: value})))
        self.reject_measurement(mutations)

    def test_malformed_nested_measurement_collections_reject_without_crashing(self) -> None:
        self.reject_measurement([
            ("session_ids", lambda m: m.update(session_ids=["manager"])),
            ("session_id", lambda m: m["session_ids"].update(manager=[])),
            ("window_collection", lambda m: m.update(notice_windows={"worker": {}})),
            ("window_role", lambda m: m["notice_windows"][0].update(role=[])),
            ("hold_collection", lambda m: m.update(child_hold_interval_ns={"start": 1, "end": 2})),
        ])

    def test_extreme_integer_delays_reject_without_overflowing_judge(self) -> None:
        self.reject_measurement([
            ("reported_max", lambda m: m.update(max_enqueue_to_start_ms=10 ** 400)),
            ("reported_delta", lambda m: m["notice_windows"][0].update(enqueue_to_start_ms=10 ** 400)),
            ("observed_time", lambda m: m["notice_windows"][0].update(agent_end_ns=10 ** 400)),
        ])

    def test_lost_clock_due_status_and_mutex_limitation_reject(self) -> None:
        self.reject_measurement([
            ("clock", lambda m: m.update(clock="time.time_ns")),
            ("kind", lambda m: m.update(kind="claimed_overlap")),
            ("due_status", lambda m: m.update(due_scheduler_status="deferred")),
            ("lock_inferred", lambda m: m.update(internal_lock_wait="zero")),
        ])

    def test_unobserved_sharing_or_inferred_mutex_duration_rejects(self) -> None:
        for changes in ({"shared_session_or_lock": None}, {"shared_session_or_lock": "false"},
                        {"shared_session_or_lock": 0}, {"measured_block_ms": 0}, {"max_block_recorded": True}):
            facts = nonshared_facts()
            facts.update(changes)
            with self.subTest(changes=changes):
                self.assertNotEqual(self.verdict(facts)["result"], "passed")

    def test_explicit_safety_violation_fails_and_missing_priority_is_inconclusive(self) -> None:
        for field in ("busy_deferred", "user_priority", "due_check_priority", "end_priority", "independent_delivery_observed"):
            facts = nonshared_facts()
            facts[field] = False
            with self.subTest(field=field):
                self.assertEqual(self.verdict(facts)["result"], "failed")
            facts.pop(field)
            self.assertEqual(self.verdict(facts)["result"], "inconclusive")

    def test_shared_requires_numeric_measure_and_adoption(self) -> None:
        facts = nonshared_facts()
        facts.update(shared_session_or_lock=True, max_block_recorded=True, measured_block_ms=120)
        self.assertEqual(self.verdict(facts)["result"], "blocked_adoption")
        facts["user_adoption_ref"] = "explicit-user-decision"
        self.assertEqual(self.verdict(facts)["result"], "passed")
        for value in (None, True, "120", -1, float("nan"), float("inf")):
            facts["measured_block_ms"] = value
            with self.subTest(value=value):
                self.assertEqual(self.verdict(facts)["result"], "inconclusive")

    def test_row_and_envelope_unknown_propagate_without_other_seven_regression(self) -> None:
        rows = rows_fixture()
        next(row for row in rows if row["id"] == "Q01-priority")["unknowns"] = ["lost_priority_event"]
        verdicts = judge_envelope({"observations": rows, "unknowns": []}, adapter="probe-local")
        self.assertEqual(verdicts["Q01-priority"]["result"], "inconclusive")
        self.assertTrue(all(v["result"] == "passed" for k, v in verdicts.items() if k != "Q01-priority"))
        verdicts = judge_envelope({"observations": rows_fixture(), "unknowns": ["custody_unknown"]}, adapter="probe-local")
        self.assertTrue(all(v["result"] == "inconclusive" and "custody_unknown" in v["unknowns"] for v in verdicts.values()))


if __name__ == "__main__":
    unittest.main()
