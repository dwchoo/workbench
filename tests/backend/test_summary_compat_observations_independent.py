"""Partial observations stay truthful; controlled fixtures prove no Native/SDK pass."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.backend import test_summary_compat_provider as fixtures
from workbench.backend import summary_compat_provider as provider
from workbench.backend import summary_compat_runtime as runtime
from workbench.backend.summary_compatibility import SummaryCompatibility


class CompatibilityObservationsIndependent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        output = os.environ.get("WB_DVL_PRECISE_TEST_OUTPUT")
        if output and not Path(output).is_absolute():
            raise AssertionError("fixture output must be absolute before import")
        temporary = tempfile.TemporaryDirectory(prefix="partial-observations-", dir=output)
        cls.addClassCleanup(temporary.cleanup)
        cls.parent = Path(temporary.name)
        # Reuse only the existing external Popen fixture and real fixed writer/DB.
        # Its assertions/claims are not the oracle for the expectations below.
        helper = fixtures.OwnedC1Tests("runTest")
        helper.root = cls.parent / "c1"
        helper.root.mkdir(mode=0o700)
        helper.binary = helper.root / "omp"
        helper.binary.write_bytes(b"controlled external launch; never executed")
        _, _, cls.completed_observation = helper.run_double()

    def setUp(self):
        self.observed = deepcopy(self.completed_observation)
        self.native = patch.object(runtime.subprocess, "Popen", side_effect=AssertionError("Native launch forbidden"))
        self.native.start()
        self.addCleanup(self.native.stop)

    @staticmethod
    def projected(observed):
        return {row["item"]: row for row in runtime._item_rows(observed)}

    def test_execute_keeps_confirmed_writer_checks_and_unperformed_items(self):
        raw = deepcopy(self.observed["observations"])
        rows = {row["item"]: row for row in self.observed["item_rows"]}
        self.assertEqual(set(rows), {"Q01-route", "Q01-tools", "Q01-skill", "Q01-result",
                                    "Q01-context", "Q01-priority", "Q01-recovery", "Q01-origin"})
        result = rows["Q01-result"]
        self.assertEqual(result["result"], "inconclusive")
        for name in ("owned_c1_cleanup_complete", "writer_request_binding_matches", "receipt_matches",
                     "body_matches_fixture", "single_catalog_entry"):
            self.assertIs(result["checks"][name], True, name)
        self.assertEqual(rows["Q01-route"]["result"], "inconclusive")
        self.assertIsNone(rows["Q01-route"]["checks"]["parent_unchanged"])
        self.assertEqual(rows["Q01-priority"]["result"], "not_run")
        self.assertEqual(rows["Q01-recovery"]["result"], "not_run")
        self.assertEqual(rows["Q01-result"]["evidence_refs"][0]["observation_index"], 0)
        self.assertEqual(self.projected(self.observed), rows)
        self.assertEqual(self.observed["observations"], raw)
        self.assertEqual(self.observed["level"], "inconclusive")
        for name in ("network_requests", "auth_store_open", "settings_writes"):
            self.assertIsNone(self.observed["io"][name])

    def test_known_parent_tool_or_request_binding_violation_is_failed(self):
        for violation, item in (("parent", "Q01-route"), ("tool", "Q01-tools"), ("binding", "Q01-result")):
            with self.subTest(violation=violation):
                observed = deepcopy(self.observed)
                c1 = observed["observations"][0]
                if violation == "parent":
                    c1["completed"]["parentUnchanged"] = False
                    check = "parent_unchanged"
                elif violation == "tool":
                    c1["localhost_model_posts"] = [{"tools": ["db_summary_save", "shell"], "system_exact": True}]
                    check = "observed_post_used_db_summary_save_only"
                else:
                    c1["completed"]["writerObserved"]["request_binding_matches"] = False
                    check = "writer_request_binding_matches"
                row = self.projected(observed)[item]
                self.assertIs(row["checks"][check], False)
                self.assertEqual(row["result"], "failed")

    def test_unknown_checks_and_absent_completion_are_not_confirmed_failures(self):
        c1 = self.observed["observations"][0]
        c1["completed"]["parentUnchanged"] = None
        c1["completed"]["childDisposed"] = None
        c1["completed"]["writerObserved"]["request_binding_matches"] = None
        rows = self.projected(self.observed)
        self.assertIsNone(rows["Q01-route"]["checks"]["parent_unchanged"])
        self.assertIsNone(rows["Q01-tools"]["checks"]["child_disposed"])
        self.assertIsNone(rows["Q01-result"]["checks"]["writer_request_binding_matches"])
        self.assertEqual({row["result"] for row in rows.values()}, {"inconclusive", "not_run"})
        c1["completed"] = None
        self.assertEqual({row["result"] for row in self.projected(self.observed).values()}, {"not_run"})

    def test_provider_preserves_priority_recovery_raw_facts_and_unknown_io(self):
        c1 = self.observed["observations"][0]
        c1["recovery_control"] = {"unknown_before_response": True, "replay_reserve_refused": True,
            "replay_row_present": False, "duplicate_receipt_accepted": False, "receipt_unchanged": True,
            "state_order": ["sending", "unknown", "stored"],
            "owner_predicate_false_control": {"noSendConfirmed": None}}
        self.observed["item_rows"] = runtime._item_rows(self.observed)
        raw_c1 = deepcopy(c1)
        priority = {"kind": "owned_product_priority_partial", "result": "inconclusive",
                    "native_launches": 1, "coverage_complete": False, "cleanup_complete": True,
                    "measured_blocking_ms": 12, "scenarios": ["composer_busy"], "errors": []}
        with patch.object(provider, "observe_owned", return_value=self.observed), \
                patch.object(provider, "observe_priority", return_value=priority):
            result = provider.ScriptedCompatibilityProvider().observe()
        self.assertEqual(result["observations"], [raw_c1, priority])
        self.assertTrue(all(value is None for value in result["io"].values()))
        rows = {row["item"]: row for row in result["item_rows"]}
        self.assertEqual(rows["Q01-priority"]["result"], "inconclusive")
        self.assertIs(rows["Q01-priority"]["checks"]["priority_coverage_complete"], False)
        self.assertEqual(rows["Q01-priority"]["evidence_refs"][0]["observation_index"], 1)
        self.assertIs(rows["Q01-recovery"]["checks"]["recovery_replay_refused"], True)
        self.assertIs(rows["Q01-recovery"]["checks"]["recovery_replay_row_absent"], True)
        self.assertIsNone(rows["Q01-recovery"]["checks"]["pause_no_send_confirmed"])
        self.assertEqual(rows["Q01-recovery"]["result"], "inconclusive")

    def test_explicit_rows_survive_record_and_return_without_bare_pass_or_admission(self):
        identity = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64,
                    "closure_digest": "2" * 64, "runtime_digest": "3" * 64,
                    "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
        service = SummaryCompatibility(self.parent / "compat", identity=lambda: deepcopy(identity))
        service.protect({"episode": "prior", "reason": "compatibility_failed"})
        self.observed["items"] = {name: "pass" for name in runtime.Q01_ITEMS}
        supplied = [
            {"item": "Q01-route", "result": "inconclusive", "checks": {"parent_unchanged": True},
             "unknowns": ["role_filter_controls_unobserved"], "evidence_refs": [{"observation_index": 0}]},
            {"item": "Q01-tools", "result": "failed", "checks": {"only_db_tool": False},
             "unknowns": [], "evidence_refs": [{"observation_index": 0}]},
            {"item": "Q01-result", "result": "pass", "checks": {"binding": True},
             "unknowns": [], "evidence_refs": [{"observation_index": 0}]},
            {"item": "Q01-recovery", "result": "not_run", "checks": {"late_result": None},
             "unknowns": ["item_not_observed"], "evidence_refs": []},
        ]
        self.observed["item_rows"] = deepcopy(supplied)
        self.observed["io"] = dict.fromkeys(runtime.IO_KEYS)
        raw = deepcopy(self.observed)
        with patch.object(provider.ScriptedCompatibilityProvider, "observe", return_value=self.observed):
            result = service.verify_scripted()
        record = json.loads((service.directory / (result["id"] + ".json")).read_bytes())
        for key, value in result.items():
            self.assertEqual(record[key], value, key)
        rows = {row["item"]: row for row in result["item_rows"]}
        for row in supplied:
            self.assertEqual(rows[row["item"]], row)
        self.assertEqual(result["items"]["Q01-origin"], "not_run")  # A bare pass is not evidence.
        self.assertEqual(result["result"], "failed")
        self.assertEqual(record["observations"], raw["observations"])
        self.assertEqual(record["io"], raw["io"])
        self.assertFalse(service.verify_reference(result["id"]))
        self.assertFalse(service.admission())
        self.assertEqual(service.status()["reason"], "compatibility_failed")

    def test_rejected_bad_tools_post_without_completion_remains_failed_in_record(self):
        raw = {"kind": "owned_C1_partial", "cleanup_complete": True, "completed": None,
               "localhost_model_posts": [{"tools": ["bash"], "system_exact": True}]}
        self.observed["observations"] = [deepcopy(raw)]
        self.observed["items"] = {}
        self.observed["item_rows"] = runtime._item_rows(self.observed)
        row = {row["item"]: row for row in self.observed["item_rows"]}["Q01-tools"]
        self.assertIs(row["checks"]["observed_post_used_db_summary_save_only"], False)
        self.assertIsNone(row["checks"]["c1_case_exercised"])
        self.assertEqual(row["result"], "failed")
        self.assertEqual(self.observed["observations"], [raw])
        identity = {"policy_version": 1, "unknowns": [], "policy_sha256": "1" * 64,
                    "closure_digest": "2" * 64, "runtime_digest": "3" * 64,
                    "stable_host": {"os_family": "Linux", "architecture": "x86_64", "python_minor": "3.12"}}
        service = SummaryCompatibility(self.parent / "rejected-tools", identity=lambda: deepcopy(identity))
        with patch.object(provider.ScriptedCompatibilityProvider, "observe", return_value=self.observed):
            result = service.verify_scripted()
        record = json.loads((service.directory / (result["id"] + ".json")).read_bytes())
        self.assertEqual(result["items"]["Q01-tools"], "failed")
        self.assertEqual(record["item_rows"], result["item_rows"])
        self.assertEqual(record["observations"], [raw])
        self.assertNotEqual(record["result"], "pass")
        self.assertFalse(service.verify_reference(result["id"]))
        self.assertFalse(service.admission())

    def test_priority_parent_violations_fail_but_partial_timeout_stays_unknown(self):
        def observe_priority(priority):
            with patch.object(provider, "observe_owned", return_value=deepcopy(self.observed)), \
                    patch.object(provider, "observe_priority", return_value=deepcopy(priority)):
                result = provider.ScriptedCompatibilityProvider().observe()
            self.assertEqual(result["observations"][-1], priority)
            return {row["item"]: row for row in result["item_rows"]}["Q01-priority"]

        base = {"kind": "owned_product_priority_partial", "result": "inconclusive",
                "native_launches": 1, "coverage_complete": False, "cleanup_complete": True,
                "provider_violations": [], "errors": []}
        before = {"model": {"id": "parent"}, "thinkingLevel": "high", "serviceTierByFamily": {},
                  "registry": [], "settings": {}, "roster": [], "rawUpperMarkerSeen": False,
                  "allEntries": [{"id": "prior-entry", "text": "original"}]}
        for field, bad_value, check in (
                ("strict_parent_unchanged", False, "strict_parent_unchanged"),
                ("raw_parent_contamination", True, "raw_parent_contamination_absent"),
                ("prior_entry_views_unchanged", False, "prior_entry_views_unchanged")):
            with self.subTest(field=field):
                after = deepcopy(before)
                if field == "strict_parent_unchanged":
                    after["model"]["id"] = "changed-parent"
                elif field == "raw_parent_contamination":
                    after["rawUpperMarkerSeen"] = True
                else:
                    after["allEntries"][0]["text"] = "changed-prior-entry"
                parent = {"before": deepcopy(before), "after": after, "strict_parent_unchanged": True,
                          "raw_parent_contamination": False, "prior_entry_views_unchanged": True}
                parent[field] = bad_value
                row = observe_priority({**base, "parents": {"manager": parent}})
                self.assertEqual(row["result"], "failed")
                self.assertIs(row["checks"][check], False)

        exact_violation = {**base, "parents": {},
                           "provider_violations": ["ValueError:parent_tools_or_upper_contamination"]}
        row = observe_priority(exact_violation)
        self.assertEqual(row["result"], "failed")
        self.assertIs(row["checks"]["parent_tools_or_upper_contamination_absent"], False)
        partial = {"before": deepcopy(before), "after": None, "strict_parent_unchanged": None,
                   "raw_parent_contamination": None, "prior_entry_views_unchanged": None}
        for parents in ({}, {"manager": partial}):
            with self.subTest(partial_parent=bool(parents)):
                row = observe_priority({**base, "parents": parents, "errors": ["OSError:timed out"],
                                        "provider_violations": ["OSError:timed out", "ValueError:probe_unavailable"]})
                self.assertEqual(row["result"], "inconclusive")
                for check in ("strict_parent_unchanged", "raw_parent_contamination_absent", "prior_entry_views_unchanged"):
                    self.assertIsNone(row["checks"][check])

        # Reuse the synthetic raw control fixture, not a native pass claim.
        # All safety/priority facts are known here so the diagnostic itself
        # must prevent pass, without promoting a transient exception to failed.
        from tests.backend.summary_compat_record_fixture import complete_record
        complete = complete_record({"model_metadata": {}, "omp": {}})["observations"][1]
        complete["provider_violations"] = []
        positive = observe_priority(complete)
        self.assertEqual(positive["result"], "pass")
        self.assertTrue(all(value is True for value in positive["checks"].values()))
        for diagnostic in ("TimeoutError:owned_busy_parent_release_timeout",
                           "BrokenPipeError:[Errno 32] Broken pipe"):
            with self.subTest(transient_provider_diagnostic=diagnostic):
                observed = {**complete, "errors": [], "provider_violations": [diagnostic]}
                row = observe_priority(observed)
                self.assertEqual(row["result"], "inconclusive")
                self.assertTrue(all(value is True for value in row["checks"].values()))
                self.assertEqual(observed["errors"], [])
                self.assertEqual(observed["provider_violations"], [diagnostic])


if __name__ == "__main__":
    unittest.main()
