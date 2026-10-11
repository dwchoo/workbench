from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest

from workbench.backend.summary_writer import store_summary
from workbench.storage.important_log import ImportantLogReader
from workbench.tasks.repository import TaskRepository
from tests.backend.test_summary_adapter import request_fixture


class SummaryWriterTests(unittest.TestCase):
    def test_normal_store_preserves_gaps_and_receipt_prevents_second_append(self):
        with TemporaryDirectory() as name:
            root = Path(name)
            request, _ = request_fixture(root, ["work"])
            # The fixture used logs; product's fixed data layout uses important-logs.
            (root / "logs").rename(root / "important-logs")
            request["sourceGaps"] = [{"schemaVersion": 1, "source": request["sources"][0]["source"],
                                      "start": 4, "end": 8, "reason": "evicted"}]
            from workbench.contracts.logs_v1 import summary_input_key
            request["inputKey"] = summary_input_key(request["subject"], None, request["sources"],
                                                    request["sourceGaps"], request["skillDigest"])
            with TaskRepository(root / "tasks.sqlite3") as journal:
                os.chmod(root / "tasks.sqlite3", 0o600)
                journal.reserve_summary_request(request)
                journal.record_summary_state(request["requestId"], "sending")
            result = {"schemaVersion": 1, "requestId": request["requestId"],
                      "inputKey": request["inputKey"], "skillDigest": request["skillDigest"],
                      "sources": request["sources"], "facts": ["verified"], "interpretations": [], "unknowns": [],
                      "usage": {"schemaVersion": 1, "inputTokens": None, "outputTokens": None, "costUsd": None}}
            first = store_summary(root, request["requestId"], result)
            self.assertTrue(first["saved"])
            second = store_summary(root, request["requestId"], result)
            self.assertFalse(second["saved"])
            rows = ImportantLogReader(root / "important-logs").show(request["subject"])["entries"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["entry"]["sourceGaps"], request["sourceGaps"])
            with TaskRepository(root / "tasks.sqlite3") as journal:
                record = journal.get_summary_request(request["requestId"])
                self.assertEqual(record["state"], "stored")
                self.assertEqual(record["receipt"]["entry_id"], first["entryId"])

    def test_unknown_request_does_not_allocate_a_new_important_db(self):
        with TemporaryDirectory() as name:
            root = Path(name)
            request, _ = request_fixture(root, ["work"])
            (root / "logs").rename(root / "important-logs")
            with TaskRepository(root / "tasks.sqlite3"):
                os.chmod(root / "tasks.sqlite3", 0o600)
            before = sorted(p.name for p in (root / "important-logs").iterdir())
            result = store_summary(root, request["requestId"], {})
            self.assertFalse(result["saved"])
            self.assertEqual(before, sorted(p.name for p in (root / "important-logs").iterdir()))


if __name__ == "__main__":
    unittest.main()
