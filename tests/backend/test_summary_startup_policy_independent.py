"""LG-D23 judge fixtures; these are not actual OMP Q01 evidence."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "tools"))
from q01_recheck import REQUIRED_FACTS, judge_observations  # noqa: E402


# Expected from approved SPEC §8.1 / LG-D23, independently of the judge table.
TOOLS_FACTS = (
    "receipt_request_bound", "session_turn_epoch_bound", "receipt_route_bound",
    "policy_nonce_bound", "startup_private_config_verified",
    "startup_sole_db_tool_readback", "startup_guard_effective_verified",
    "startup_missing_guard_prompt_sent_zero", "startup_noop_guard_prompt_sent_zero",
    "startup_api_mismatch_prompt_sent_zero", "startup_stale_receipt_prompt_sent_zero",
    "startup_failed_session_disposed", "startup_rebuild_once_reverified",
    "startup_repeated_failure_prompt_sent_zero",
    "startup_repeated_failure_summary_deferred",
    "startup_repeated_failure_main_continues", "runtime_binding_pre_dispatch",
    "runtime_request_mismatch_sent_zero", "runtime_roster_mismatch_sent_zero",
    "runtime_model_mismatch_sent_zero", "runtime_policy_mismatch_sent_zero",
    "malicious_tools_blocked", "sentinel_unchanged",
    "post_receipt_violation_rejected", "forbidden_event_immediate_protection",
    "upper_turn_unaffected", "normal_permissions_restored", "no_extra_model_turn",
)
OTHER_FACTS = {
    "Q01-route": ("existing_agent", "isolated_turn", "typed_return", "no_new_agent", "no_standing_process"),
    "Q01-skill": ("exact_bytes_in_context", "digest_matched", "role_filter_enforced", "isolated_skill_source"),
    "Q01-result": ("typed_channel", "wrong_id_rejected", "wrong_revision_rejected",
                   "wrong_range_rejected", "duplicate_rejected"),
    "Q01-context": ("three_turns_observed", "parent_receipt_within_4k", "result_within_8k",
                    "body_within_32k", "collector_within_1m", "token_preflight",
                    "output_ceiling_enforced", "full_output_reserved", "no_main_raw_accumulation"),
    "Q01-priority": ("busy_deferred", "user_priority", "due_check_priority",
                     "end_priority", "max_block_recorded"),
    "Q01-recovery": ("pause_blocks_new", "disconnect_reconciled", "restart_reconciled",
                     "late_result_bound", "unknown_not_replayed"),
    "Q01-origin": ("task_grant_bound", "command_start_bound", "work_source_ranges_bound",
                   "terminal_close_bound", "unrelated_scope_rejected", "notice_not_authority",
                   "user_role_not_authority", "pause_revoke_preserved", "end_once_no_replay"),
}
ALL_FACTS = {"Q01-tools": TOOLS_FACTS, **OTHER_FACTS}


def fixture_rows(*, adapter: str = "probe-local") -> list[dict]:
    source = "product_adapter" if adapter == "product" else "omp_event"
    rows = [{"id": item, "adapter": adapter, "source": source,
             "evidence_level": "runtime", "evidence_ref": "synthetic judge fixture only",
             "facts": {fact: True for fact in facts}, "unknowns": []}
            for item, facts in ALL_FACTS.items()]
    priority = next(row for row in rows if row["id"] == "Q01-priority")
    priority["facts"].update(shared_session_or_lock=False, measured_block_ms=0,
                             independent_delivery_observed=True)
    return rows


def verdict(rows: list[dict], item: str, *, adapter: str = "probe-local") -> dict:
    return judge_observations(rows, adapter=adapter)[item]


class StartupJudgeContractTests(unittest.TestCase):
    def test_exact_startup_contract_and_other_seven_unchanged(self) -> None:
        self.assertEqual(len(TOOLS_FACTS), 28)
        self.assertEqual(set(REQUIRED_FACTS), set(ALL_FACTS))
        self.assertEqual(REQUIRED_FACTS["Q01-tools"], TOOLS_FACTS)
        for item, expected in OTHER_FACTS.items():
            with self.subTest(item=item):
                self.assertEqual(REQUIRED_FACTS[item], expected)
        self.assertEqual(verdict(fixture_rows(), "Q01-tools")["result"], "passed")

    def test_every_startup_and_runtime_fact_false_fails(self) -> None:
        for fact in TOOLS_FACTS:
            rows = fixture_rows()
            rows[0]["facts"][fact] = False
            with self.subTest(fact=fact):
                result = verdict(rows, "Q01-tools")
                self.assertEqual(result["result"], "failed")
                self.assertIn(fact, result["unknowns"])

    def test_origin_requires_work_and_source_facts_even_with_legacy_direct_claim(self) -> None:
        for fact in ("command_start_bound", "work_source_ranges_bound",
                     "unrelated_scope_rejected", "end_once_no_replay"):
            rows = fixture_rows()
            origin = next(row for row in rows if row["id"] == "Q01-origin")
            origin["facts"][fact] = False
            origin["facts"]["direct_command_bound"] = True
            with self.subTest(fact=fact):
                self.assertEqual(verdict(rows, "Q01-origin")["result"], "failed")

    def test_missing_unknown_or_nonboolean_startup_evidence_cannot_pass(self) -> None:
        critical = ("startup_private_config_verified", "startup_sole_db_tool_readback",
                    "startup_guard_effective_verified", "startup_missing_guard_prompt_sent_zero",
                    "startup_noop_guard_prompt_sent_zero", "startup_api_mismatch_prompt_sent_zero",
                    "startup_stale_receipt_prompt_sent_zero", "startup_failed_session_disposed",
                    "startup_rebuild_once_reverified", "startup_repeated_failure_prompt_sent_zero",
                    "startup_repeated_failure_summary_deferred",
                    "startup_repeated_failure_main_continues")
        for fact in critical:
            for value in (None, "unknown", 1):
                rows = fixture_rows()
                rows[0]["facts"][fact] = value
                with self.subTest(fact=fact, value=value):
                    self.assertEqual(verdict(rows, "Q01-tools")["result"], "inconclusive")
            rows = fixture_rows()
            del rows[0]["facts"][fact]
            with self.subTest(fact=fact, value="missing"):
                self.assertEqual(verdict(rows, "Q01-tools")["result"], "inconclusive")

    def test_fixture_level_self_report_or_unknown_cannot_validate(self) -> None:
        for mutation in (
            {"evidence_level": "fixture"},
            {"source": "model_self_report"},
            {"unknowns": ["startup_not_observed"]},
            {"evidence_ref": ""},
        ):
            rows = fixture_rows()
            rows[0].update(mutation)
            with self.subTest(mutation=mutation):
                self.assertNotEqual(verdict(rows, "Q01-tools")["result"], "passed")
        rows = fixture_rows(adapter="product")
        rows[0]["source"] = "omp_event"
        self.assertNotEqual(verdict(rows, "Q01-tools", adapter="product")["result"], "passed")

    def test_other_seven_judge_behaviors_still_isolated(self) -> None:
        baseline = judge_observations(fixture_rows(), adapter="probe-local")
        self.assertTrue(all(v["result"] == "passed" for v in baseline.values()))
        for item, facts in OTHER_FACTS.items():
            rows = fixture_rows()
            row = next(r for r in rows if r["id"] == item)
            row["facts"][facts[0]] = False
            outcomes = judge_observations(rows, adapter="probe-local")
            with self.subTest(item=item):
                self.assertEqual(outcomes[item]["result"], "failed")
                self.assertEqual(outcomes["Q01-tools"]["result"], "passed")
                self.assertTrue(all(outcomes[other]["result"] == "passed"
                                    for other in OTHER_FACTS if other != item))
        # Product provenance and sharing policy retain their separate gates.
        rows = fixture_rows(adapter="product")
        rows[0]["source"] = "omp_event"
        self.assertNotEqual(verdict(rows, "Q01-tools", adapter="product")["result"], "passed")
        rows = fixture_rows(adapter="product")
        priority = next(r for r in rows if r["id"] == "Q01-priority")
        priority["facts"]["shared_session_or_lock"] = True
        self.assertEqual(verdict(rows, "Q01-priority", adapter="product")["result"],
                         "blocked_adoption")


if __name__ == "__main__":
    unittest.main()
