"""Real private catalog + request journal; no paid model or user DB."""
from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import LogContractError, canonical_json
from workbench.storage.important_log import ImportantLogCatalog, ImportantLogReader
from workbench.tasks.repository import InvalidTransitionError, TaskRepository
from tests.backend.test_summary_adapter_independent import contract_fixture, rebind


def create_private_case(root: Path, *, state="sending"):
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    request, metadata, result = contract_fixture(("work",))
    catalog = ImportantLogCatalog(root / "important-logs", create=True)
    request["dbHealth"] = catalog.health_snapshot()["active"]
    request["sourceGaps"] = [{"schemaVersion": 1, "source": deepcopy(request["sources"][0]["source"]),
                              "start": 4, "end": 9, "reason": "evicted"}]
    rebind(request)
    result["inputKey"] = request["inputKey"]
    with TaskRepository(root / "tasks.sqlite3") as journal:
        os.chmod(root / "tasks.sqlite3", 0o600)
        journal.reserve_summary_request(request)
        if state != "prepared":
            journal.record_summary_state(request["requestId"], "sending")
        if state in {"executed_observed", "rejected"}:
            journal.record_summary_state(request["requestId"], "executed_observed")
        if state == "rejected":
            journal.record_summary_state(request["requestId"], "rejected", detail="guard_violation_observed")
        if state in {"unknown", "storage_failed"}:
            journal.record_summary_state(request["requestId"], "unknown", detail="owned dispatch timeout")
        if state == "storage_failed":
            journal.record_summary_state(request["requestId"], "storage_failed")
    return {"request": request, "metadata": metadata, "result": result}


def observe_private_case(root: Path, request):
    rows = ImportantLogReader(root / "important-logs").show(request["subject"])["entries"]
    with TaskRepository(root / "tasks.sqlite3") as journal:
        record = journal.get_summary_request(request["requestId"])
    return {"entries": [row["entry"] for row in rows], "record": record}


class IndependentSummaryWriterTests(unittest.TestCase):
    def test_normal_first_store_and_duplicate_never_reappend_and_preserve_bound_gaps(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            case = create_private_case(root)
            result = case["result"]
            first = store_summary(root, result["requestId"], result)
            self.assertTrue(first["saved"])
            self.assertFalse(store_summary(root, result["requestId"], result)["saved"])
            observed = observe_private_case(root, case["request"])
            self.assertEqual(len(observed["entries"]), 1)
            entry = observed["entries"][0]
            self.assertEqual(entry["subject"], case["request"]["subject"])
            self.assertEqual(entry["sources"], case["request"]["sources"])
            self.assertEqual(entry["sourceGaps"], case["request"]["sourceGaps"])
            self.assertEqual(entry["body"]["facts"], result["facts"])
            self.assertEqual(observed["record"]["state"], "stored")
            self.assertEqual(observed["record"]["receipt"]["entry_id"], first["entryId"])
            from hashlib import sha256
            self.assertEqual(observed["record"]["receipt"]["result_digest"], sha256(canonical_json(result)).hexdigest())

    def test_prepared_and_rejected_cannot_commit_or_change_last_normal_entry(self):
        for state in ("prepared", "rejected"):
            with self.subTest(state=state), TemporaryDirectory() as directory:
                root = Path(directory)
                case = create_private_case(root, state=state)
                request = case["request"]
                catalog = ImportantLogCatalog(root / "important-logs", create=False)
                previous = catalog.append(request["subject"], "cumulative", request["sources"],
                                          {"schemaVersion": 1, "facts": ["last good"],
                                           "interpretations": [], "unknowns": []})["entry"]
                outcome = store_summary(root, request["requestId"], case["result"])
                self.assertFalse(outcome["saved"])
                observed = observe_private_case(root, request)
                self.assertEqual(observed["entries"], [previous])
                self.assertIsNone(observed["record"]["receipt"])
                self.assertEqual(observed["record"]["state"], state)
                with TaskRepository(root / "tasks.sqlite3") as journal:
                    with self.assertRaises(InvalidTransitionError):
                        journal.record_summary_result_receipt(case["result"], str(uuid4()))

    def test_valid_late_result_from_unknown_is_stored_once_without_a_model_call(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            case = create_private_case(root, state="unknown")
            outcome = store_summary(root, case["request"]["requestId"], case["result"])
            self.assertTrue(outcome["saved"])
            self.assertEqual(observe_private_case(root, case["request"])["record"]["state"], "stored")

    def test_invalid_identity_and_utf8_envelope_do_not_insert_any_entry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            case = create_private_case(root)
            for result in ({**case["result"], "inputKey": "0" * 64},
                           {**case["result"], "facts": ["한" * 2700]},
                           {**case["result"], "sql": "DELETE FROM important_entries"}):
                with self.subTest(keys=list(result)), self.assertRaises(LogContractError):
                    store_summary(root, case["request"]["requestId"], result)
                observed = observe_private_case(root, case["request"])
                self.assertEqual(observed["entries"], [])
                self.assertIsNone(observed["record"]["receipt"])

    def test_committed_entry_receipt_failure_is_reported_durable_no_retry_append(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            case = create_private_case(root)
            with patch.object(TaskRepository, "record_summary_result_receipt", side_effect=OSError("journal I/O")):
                outcome = store_summary(root, case["request"]["requestId"], case["result"])
            self.assertFalse(outcome["saved"])
            self.assertEqual(outcome["writeOutcome"], "durable")
            self.assertFalse(outcome["retryAppend"])
            self.assertEqual(outcome["reason"], "receipt_outcome_unknown")
            observed = observe_private_case(root, case["request"])
            self.assertEqual(len(observed["entries"]), 1)
            self.assertEqual(observed["entries"][0]["entryId"], outcome["durableEntryId"])
            self.assertIsNone(observed["record"]["receipt"])
            # Caller must honor retryAppend:false. No second invocation is made here.

    def test_unsafe_private_journal_is_rejected_before_catalog_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            case = create_private_case(root)
            os.chmod(root / "tasks.sqlite3", 0o644)
            with self.assertRaisesRegex(ValueError, "unsafe_summary_storage"):
                store_summary(root, case["request"]["requestId"], case["result"])
            self.assertEqual(observe_private_case(root, case["request"])["entries"], [])


if __name__ == "__main__":
    unittest.main()
