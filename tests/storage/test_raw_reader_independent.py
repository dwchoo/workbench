"""Independent source-framing, loss, and bounded-read checks for raw-v2."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zlib

from workbench.contracts.logs_v1 import parse_raw_read_result
from workbench.storage.log_raw import SegmentIntegrityError, SegmentedRawStore


TIME = "2026-01-02T03:04:05Z"
FILE_HEADER = struct.Struct(">8sQ")
FRAME_HEADER = struct.Struct(">4sHII")


def source(*, command_id: str | None = None, epoch: int = 1) -> dict[str, object]:
    return {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": epoch,
            "commandId": command_id or str(uuid4())}


class IndependentRawReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owned = tempfile.TemporaryDirectory(prefix="dvl11-reader-independent-", dir="/dev/shm")
        self.addCleanup(self.owned.cleanup)
        self.root = Path(self.owned.name) / "raw"

    def test_binary_and_utf8_bytes_keep_physical_source_frame_integrity(self) -> None:
        ref = source()
        body = bytes(range(256)) * 260 + "한글".encode("utf-8") * 500
        self.assertGreater(len(body), 65536)
        with SegmentedRawStore(self.root, segment_limit=70_000, segment_count=5) as store:
            receipt = store.append(ref, body, observed_at=TIME, source_offset=0)
            self.assertEqual(receipt["stored_bytes"], len(body))
            self.assertEqual(receipt["missing_bytes"], 0)
            reconstructed = bytearray()
            next_offset = 0
            frame_count = 0
            for path in sorted(self.root.glob("seg-*.raw")):
                raw = path.read_bytes()
                self.assertEqual(FILE_HEADER.unpack_from(raw)[0], b"WBRAW2\0\0")
                self.assertLessEqual(len(raw), 70_000)
                cursor = FILE_HEADER.size
                while cursor < len(raw):
                    magic, meta_size, data_size, crc = FRAME_HEADER.unpack_from(raw, cursor)
                    self.assertEqual(magic, b"FRM2")
                    self.assertLessEqual(data_size, 65536)
                    meta_start = cursor + FRAME_HEADER.size
                    meta_raw = raw[meta_start:meta_start + meta_size]
                    data = raw[meta_start + meta_size:meta_start + meta_size + data_size]
                    self.assertEqual(len(data), data_size)
                    self.assertEqual(zlib.crc32(data, zlib.crc32(meta_raw)) & 0xffffffff, crc)
                    meta = json.loads(meta_raw)
                    self.assertEqual(meta["source"], ref)
                    self.assertEqual(meta["time"], TIME)
                    self.assertEqual((meta["start"], meta["end"]),
                                     (next_offset, next_offset + data_size))
                    self.assertEqual(meta["digest"], sha256(data).hexdigest())
                    reconstructed.extend(data)
                    next_offset += data_size
                    frame_count += 1
                    cursor = meta_start + meta_size + data_size
                self.assertEqual(cursor, len(raw))
            self.assertGreaterEqual(frame_count, 2)
            self.assertEqual(bytes(reconstructed), body)
            page = store.read_raw(ref, 0, len(body))
            parse_raw_read_result(page)
            self.assertEqual(page["status"], "available")
            self.assertEqual(b"".join(base64.b64decode(chunk["dataBase64"])
                                      for chunk in page["chunks"]), body)
            self.assertEqual((page["available"][0]["start"], page["available"][-1]["end"]),
                             (0, len(body)))

    def test_same_command_new_collector_epoch_has_separate_offset_space(self) -> None:
        command_id = str(uuid4())
        first = source(command_id=command_id, epoch=1)
        second = source(command_id=command_id, epoch=2)
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            self.assertEqual(store.append(first, b"before", source_offset=0)["stored_bytes"], 6)
            self.assertEqual(store.append(second, b"after", source_offset=0)["stored_bytes"], 5)
            self.assertEqual(store.source_status(first)["observed_bytes"], 6)
            self.assertEqual(store.source_status(second)["observed_bytes"], 5)
        with SegmentedRawStore(self.root, segment_limit=512) as reopened:
            first_read = reopened.read_raw(first, 0, 6)
            second_read = reopened.read_raw(second, 0, 5)
            self.assertEqual(base64.b64decode(first_read["chunks"][0]["dataBase64"]), b"before")
            self.assertEqual(base64.b64decode(second_read["chunks"][0]["dataBase64"]), b"after")
            with self.assertRaises(ValueError):
                reopened.append(second, b"overlap", source_offset=0)
            self.assertEqual(reopened.source_status(second)["observed_bytes"], 5)

    def test_one_source_can_starve_another_and_eviction_is_honest(self) -> None:
        minority, majority = source(), source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as store:
            store.append(minority, b"minor", source_offset=0)
            for index in range(12):
                receipt = store.append(majority, bytes([index]) * 200,
                                       source_offset=index * 200)
                self.assertEqual((receipt["stored_bytes"], receipt["missing_bytes"]), (200, 0))
                self.assertLessEqual(len(store.segment_sizes()), 2)
            status = store.source_status(minority)
            self.assertEqual((status["observed_bytes"], status["retained_bytes"],
                              status["evicted_bytes"], status["missing_bytes"]), (5, 0, 5, 0))
            self.assertEqual(store.read_raw(minority, 0, 5)["missing"][0]["reason"], "evicted")
            dominant = store.source_status(majority)
            self.assertEqual(dominant["observed_bytes"], 2400)
            self.assertEqual(dominant["retained_bytes"] + dominant["evicted_bytes"]
                             + dominant["missing_bytes"], 2400)

    def test_reader_rejects_oversized_or_fragmented_page_and_reports_truncation(self) -> None:
        ref = source()
        with SegmentedRawStore(self.root, segment_limit=20_000) as store:
            for index in range(33):
                if index:
                    with patch.object(store, "_append_frame", side_effect=OSError(28, "fixture full")):
                        lost = store.append(ref, b"x", source_offset=2 * index - 1)
                    self.assertEqual(lost["missing_bytes"], 1)
                written = store.append(ref, b"A", source_offset=2 * index)
                self.assertEqual(written["stored_bytes"], 1)
            status = store.source_status(ref)
            self.assertEqual((status["observed_bytes"], status["retained_bytes"],
                              status["evicted_bytes"], status["missing_bytes"]), (65, 33, 0, 32))
            self.assertTrue(status["range_detail_truncated"])
            with self.assertRaises(ValueError):
                store.read_raw(ref, 0, 65)
            self.assertEqual(store.read_raw(ref, 0, 1)["status"], "available")
            for start, end in ((0, 0), (2, 1), (-1, 1), (0, 128 * 1024 + 1),
                               (False, 1)):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    store.read_raw(ref, start, end)
            never_seen = store.read_raw(source(), 0, 128 * 1024)
            self.assertEqual(never_seen["status"], "unknown")
            self.assertEqual(never_seen["missing"][0]["reason"], "unknown")

    def test_control_contains_no_body_and_rejects_forged_loss_accounting(self) -> None:
        ref = source()
        secret = b"UNIQUE-RAW-BODY-\xff\x00"
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            store.append(ref, secret, observed_at=TIME)
        control = self.root / ".control.json"
        original = control.read_bytes()
        self.assertNotIn(secret, original)
        parsed = json.loads(original)
        entry = next(iter(parsed["sources"].values()))
        entry["evicted"] = entry["high"] + 1
        forged = json.dumps(parsed, sort_keys=True, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8")
        control.write_bytes(forged)
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(self.root, segment_limit=512)
        self.assertEqual(control.read_bytes(), forged)

    def test_reader_never_marks_changed_bytes_available_after_verified_scan(self) -> None:
        ref = source()
        original = b"ORIGINAL!"
        changed = b"MUTATED!!"
        self.assertEqual(len(original), len(changed))
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            store.append(ref, original, observed_at=TIME)
            verified_scan = store._scan
            changed_on_disk = False

            def mutate_after_scan(seq: int):
                nonlocal changed_on_disk
                for metadata, payload_offset, payload_size in verified_scan(seq):
                    if not changed_on_disk:
                        path = self.root / f"seg-{seq:016d}.raw"
                        with path.open("r+b") as file:
                            file.seek(payload_offset)
                            self.assertEqual(file.read(payload_size), original)
                            file.seek(payload_offset)
                            file.write(changed)
                            file.flush()
                            os.fsync(file.fileno())
                        changed_on_disk = True
                    yield metadata, payload_offset, payload_size

            with patch.object(store, "_scan", side_effect=mutate_after_scan):
                try:
                    result = store.read_raw(ref, 0, len(original))
                except (SegmentIntegrityError, ValueError):
                    result = None
            self.assertTrue(changed_on_disk)
            if result is not None:
                self.assertNotEqual(result["status"], "available")
                self.assertEqual(result["chunks"], [])

    def test_public_name_swap_to_valid_donor_is_never_adopted_by_reader(self) -> None:
        for operation in ("read_raw", "source_status"):
            with self.subTest(operation=operation):
                root = self.owned.name + "/" + operation
                donor_root = self.owned.name + "/donor-" + operation
                ref = source()
                original, replacement = b"ORIGINAL", b"DONORRAW"
                with SegmentedRawStore(donor_root, segment_limit=512) as donor_store:
                    self.assertEqual(donor_store.append(ref, replacement, observed_at=TIME)["stored_bytes"], 8)
                donor_path, = Path(donor_root).glob("seg-*.raw")
                with SegmentedRawStore(root, segment_limit=512) as store:
                    self.assertEqual(store.append(ref, original, observed_at=TIME)["stored_bytes"], 8)
                    public, = Path(root).glob("seg-*.raw")
                    held = Path(self.owned.name) / ("held-" + operation)
                    original_bytes = public.read_bytes()
                    donor_bytes = donor_path.read_bytes()
                    real_scan = store._scan
                    swapped = False

                    def swap_before_scan(seq, *args, **kwargs):
                        nonlocal swapped
                        if not swapped:
                            public.rename(held)
                            donor_path.rename(public)
                            swapped = True
                        return real_scan(seq, *args, **kwargs)

                    with patch.object(store, "_scan", side_effect=swap_before_scan):
                        try:
                            result = (store.read_raw(ref, 0, 8) if operation == "read_raw"
                                      else store.source_status(ref))
                        except (SegmentIntegrityError, OSError, ValueError):
                            result = None
                    self.assertTrue(swapped)
                    self.assertEqual(held.read_bytes(), original_bytes)
                    self.assertEqual(public.read_bytes(), donor_bytes)
                    if result is not None:
                        self.assertNotEqual(result["status"], "available")
                        if operation == "read_raw":
                            self.assertEqual(result["chunks"], [])
                        else:
                            self.assertEqual(result["retained_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
