"""Source-relative offset and gap checks for the segmented reader."""

from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.contracts.logs_v1 import parse_raw_read_result
from workbench.storage.log_raw import SegmentedRawStore


def source() -> dict:
    return {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
            "commandId": str(uuid4())}


class RawReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dvl11-reader-", dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "raw"

    def test_chunks_preserve_source_offsets_across_segments(self):
        a, b = source(), source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=5) as store:
            store.append(a, b"A" * 300)
            store.append(b, b"B" * 100)
            store.append(a, b"C" * 100)
            result = store.read_raw(a, 250, 400)
            self.assertEqual(result["status"], "available")
            self.assertEqual(b"".join(base64.b64decode(chunk["dataBase64"])
                                      for chunk in result["chunks"]), b"A" * 50 + b"C" * 100)
            self.assertEqual(result["chunks"][0]["range"]["start"], 250)
            self.assertEqual(result["chunks"][-1]["range"]["end"], 400)
            parse_raw_read_result(result)

    def test_fault_gap_and_eviction_are_visible(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as store:
            store.append(a, b"A" * 100)
            with patch.object(store, "_append_frame", side_effect=OSError(28, "full")):
                store.append(a, b"lost")
            store.append(a, b"B" * 100)
            before = store.read_raw(a, 95, 109)
            self.assertEqual([(g["start"], g["end"], g["reason"]) for g in before["missing"]],
                             [(100, 104, "storage_gap")])
            for _ in range(6):
                store.append(a, b"C" * 100)
            after = store.read_raw(a, 0, 109)
            self.assertEqual(after["missing"][0]["reason"], "evicted")
            self.assertIn({"schemaVersion": 1, "source": a, "start": 100,
                           "end": 104, "reason": "storage_gap"}, after["missing"])
            self.assertGreater(store.source_status(a)["evicted_bytes"], 0)
            self.assertEqual(store.source_status(a)["observed_bytes"], 100 + 4 + 100 + 600)
            status = store.source_status(a)
            self.assertEqual(status["retained_bytes"] + status["evicted_bytes"]
                             + status["missing_bytes"], status["observed_bytes"])
            self.assertEqual(status["missing_bytes"], 4)

    def test_unknown_source_and_bounded_page(self):
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            missing = store.read_raw(source(), 0, 2)
            self.assertEqual(missing["status"], "unknown")
            self.assertEqual(missing["missing"][0]["reason"], "unknown")
            with self.assertRaises(ValueError):
                store.read_raw(source(), 0, 128 * 1024 + 1)


if __name__ == "__main__":
    unittest.main()
