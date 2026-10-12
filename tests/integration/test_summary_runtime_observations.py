"""Pure observation fixtures; these do not run or simulate installed SDK success."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from tests.integration.live_summary_scheduler_independent import (
    collect_scenario_observation,
    mark_dependent_scenarios_not_run,
)


class SummaryRuntimeObservationTests(unittest.TestCase):
    def test_reserved_stored_preserves_actual_journal_and_sdk_observations(self):
        status={"state":"stored","requestId":"reserved-request"}
        journal={"state":"stored","receipt":{"entry_id":"stored-entry"}}
        sdk={"status":"summary_result","sent":True,"result":{"requestId":"reserved-request"}}
        read_journal=Mock(return_value=journal)
        read_sdk=Mock(return_value=sdk)
        result=collect_scenario_observation("task_positive",status,{"streak":0},
            {"task_positive":{"requestId":"reserved-request"}},
            [{"case":"task_positive"},{"case":"task_positive"}],read_journal,read_sdk)
        read_journal.assert_called_once_with("reserved-request")
        read_sdk.assert_called_once_with("task_positive")
        self.assertIs(result["journal"],journal)
        self.assertIs(result["sdk"],sdk)
        self.assertNotIn("observation",result)

    def test_predispatch_defer_preserves_reason_without_inventing_results(self):
        cases=("task_positive","contract1","contract2","contract3","recovery_ok","contract_then_bash")
        for state in ("deferred","not_sent"):
            with self.subTest(state=state):
                status={"state":state,"reason":"input_budget_unavailable","requestId":None,"inFlight":False}
                control={"streak":0,"protection":None,"last_reason":"stored"}
                before=deepcopy((status,control))
                read_journal=Mock(side_effect=AssertionError("no journal request exists"))
                read_sdk=Mock(side_effect=AssertionError("no SDK result exists"))
                deferred=collect_scenario_observation("contract1",status,control,
                    {"task_positive":{"requestId":"earlier-request"}},
                    [{"case":"task_positive"}],read_journal,read_sdk)
                read_journal.assert_not_called()
                read_sdk.assert_not_called()
                self.assertEqual(deferred["provider_posts"],0)
                self.assertEqual(deferred["observation"],"predispatch_deferred")
                self.assertEqual(deferred["status"],status)
                self.assertNotIn("journal",deferred)
                self.assertNotIn("sdk",deferred)
                self.assertEqual((status,control),before)
                earlier={"journal":{"state":"stored"}}
                observed={"outcomes":{"task_positive":earlier,"contract1":deferred}}
                mark_dependent_scenarios_not_run(observed,cases,1)
                self.assertFalse(observed["passed"])
                self.assertEqual(observed["blocked_case"],"contract1")
                self.assertIs(observed["outcomes"]["task_positive"],earlier)
                self.assertIs(observed["outcomes"]["contract1"],deferred)
                for name in cases[2:]:
                    self.assertEqual(observed["outcomes"][name],{
                        "observation":"not_run","reason":"dependent_prior_predispatch_defer",
                        "depends_on":"contract1"})

    def test_unexpected_missing_request_remains_an_error(self):
        fixtures=(
            ({"state":"stored","requestId":None},[]),
            ({"state":"deferred","requestId":"claimed-request","reason":"input_budget_unavailable"},[]),
            ({"state":"deferred","requestId":None},[]),
            ({"state":"deferred","requestId":None,"reason":"input_budget_unavailable"},[{"case":"contract1"}]),
        )
        for status,posts in fixtures:
            with self.subTest(status=status,posts=posts):
                read_journal=Mock()
                read_sdk=Mock()
                with self.assertRaisesRegex(AssertionError,"missing_reserved_request:contract1"):
                    collect_scenario_observation("contract1",status,{}, {},posts,read_journal,read_sdk)
                read_journal.assert_not_called()
                read_sdk.assert_not_called()


if __name__=="__main__":
    unittest.main()
