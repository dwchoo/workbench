"""Owned fixture checks for the disabled segmented raw engine."""

from __future__ import annotations

from hashlib import sha256
from importlib import import_module
import json
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.log_raw import (
    RawLogStore, SegmentIntegrityError, SegmentedRawStore,
)
from workbench.storage.log_raw import segments as segment_module


def source(epoch: int = 1) -> dict:
    return {"schemaVersion": 1, "sourceKind": "terminal",
            "collectorEpoch": epoch, "commandId": str(uuid4())}


class RawSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dvl11-", dir="/dev/shm")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "v2"

    def test_exact_caps_and_shared_rotation(self):
        a, b = source(), source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=5) as store:
            for index in range(18):
                result = store.append(a if index % 2 else b, b"X" * 320)
                self.assertIsNone(result["storage_error"])
                self.assertEqual(result["stored_bytes"], 320)
                sizes = store.segment_sizes()
                self.assertLessEqual(len(sizes), 5)
                self.assertTrue(all(16 <= size <= 512 for size in sizes))
                self.assertEqual(store.storage_usage()["raw_bytes"], sum(sizes))
            self.assertLess(store.source_status(a)["retained_bytes"], store.source_status(a)["observed_bytes"])
            self.assertLess(store.source_status(b)["retained_bytes"], store.source_status(b)["observed_bytes"])
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=5) as reopened:
            self.assertLessEqual(len(reopened.segment_sizes()), 5)

    def test_record_split_and_exact_last_byte(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=70_000) as store:
            result = store.append(a, b"A" * 65_537)
            self.assertEqual(result["stored_bytes"], 65_537)
            self.assertEqual(store.read_raw(a, 0, 65_537)["status"], "available")
            lengths = [length for seq in store._names() for _, _, length in store._scan(seq)]
            self.assertGreaterEqual(len(lengths), 2)
            self.assertTrue(all(length <= 65_536 for length in lengths))
            self.assertTrue(all(size <= 70_000 for size in store.segment_sizes()))

    def test_partial_tail_repair_and_held_writer_lock(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            store.append(a, b"abc")
            with self.assertRaises(SegmentIntegrityError):
                SegmentedRawStore(self.root, segment_limit=512)
            segment = self.root / f"seg-{store._control['active']:016d}.raw"
            with segment.open("ab") as stream:
                stream.write(b"FRM2\0")
        with SegmentedRawStore(self.root, segment_limit=512) as reopened:
            self.assertEqual(reopened.read_raw(a, 0, 3)["status"], "available")
            self.assertEqual(reopened.segment_sizes(), [segment.stat().st_size])

    def test_partial_body_tail_is_discarded_without_inventing_source_bytes(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            store.append(a, b"abc")
            segment = self.root / f"seg-{store._control['active']:016d}.raw"
            before = segment.stat().st_size
            with segment.open("ab") as stream:
                stream.write(segment_module._FRAME_HEADER.pack(b"FRM2", 2, 100, 0))
                stream.write(b"{}partial")
        with SegmentedRawStore(self.root, segment_limit=512) as reopened:
            self.assertEqual(segment.stat().st_size, before)
            self.assertEqual(reopened.source_status(a)["observed_bytes"], 3)
            self.assertEqual(reopened.read_raw(a, 0, 3)["status"], "available")

    def test_rotation_intent_survives_unlink_then_process_loss(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, b"A" * 220)
            original = store._control["active"]
            with patch.object(store, "_finish_eviction", side_effect=OSError(5, "fixture crash")):
                failed = store.append(a, b"B" * 100)
            self.assertIsNotNone(failed["storage_error"])
            self.assertNotIn(original, store._names())
            self.assertIsNotNone(store._control["eviction"])
            # Stop this owner without treating the unfinished append as success.
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as reopened:
            self.assertIsNone(reopened._control["eviction"])
            self.assertEqual(reopened.source_status(a)["observed_bytes"], 320)
            self.assertEqual(reopened.read_raw(a, 0, 220)["missing"][0]["reason"], "evicted")

    def test_public_replacement_during_capture_preserves_actual_unknown_bytes(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, b"A" * 220)
            victim = next(self.root.glob("seg-*.raw"))
            original = victim.read_bytes()
            donor = original.replace(b"A" * 220, b"B" * 220)
            held = Path(self.temp.name) / "held-victim"
            real_capture = segment_module._capture_no_replace

            def replace_then_capture(root, public, quarantine):
                victim.rename(held)
                victim.write_bytes(donor)
                victim.chmod(0o600)
                real_capture(root, public, quarantine)

            with patch.object(segment_module, "_capture_no_replace", side_effect=replace_then_capture):
                result = store.append(a, b"N" * 220)
            self.assertEqual(result["stored_bytes"], 0)
            self.assertIsNotNone(result["storage_error"])
            self.assertEqual(held.read_bytes(), original)
            quarantine, = self.root.glob(".quarantine-*.raw")
            self.assertEqual(quarantine.read_bytes(), donor)
            with self.assertRaises(SegmentIntegrityError):
                SegmentedRawStore(self.root, segment_limit=512, segment_count=1)
            self.assertEqual(quarantine.read_bytes(), donor)

    def test_public_name_reuse_after_capture_preserves_donor_and_quarantine(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, b"A" * 220)
            victim = next(self.root.glob("seg-*.raw"))
            original = victim.read_bytes()
            donor = original.replace(b"A" * 220, b"B" * 220)
            real_capture = segment_module._capture_no_replace

            def capture_then_reuse(root, public, quarantine):
                real_capture(root, public, quarantine)
                victim.write_bytes(donor)
                victim.chmod(0o600)

            with patch.object(segment_module, "_capture_no_replace", side_effect=capture_then_reuse):
                result = store.append(a, b"N" * 220)
            self.assertEqual(result["stored_bytes"], 0)
            self.assertIsNotNone(result["storage_error"])
            quarantine, = self.root.glob(".quarantine-*.raw")
            self.assertEqual(quarantine.read_bytes(), original)
            self.assertEqual(victim.read_bytes(), donor)
            with self.assertRaises(SegmentIntegrityError):
                SegmentedRawStore(self.root, segment_limit=512, segment_count=1)
            self.assertEqual(victim.read_bytes(), donor)

    def test_capture_crash_recovers_once_across_repeated_restart(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, b"A" * 220)
        program = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore, segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=1)
real = segments._capture_no_replace
def stop_after_capture(root, public, quarantine):
    real(root, public, quarantine)
    os._exit(78)
segments._capture_no_replace = stop_after_capture
store.append(json.loads(sys.argv[2]), b'N' * 220)
"""
        child = self._child(program, str(self.root), json.dumps(a))
        self.assertEqual(child.returncode, 78, child.stderr)
        self.assertEqual(len(list(self.root.glob(".quarantine-*.raw"))), 1)
        for _ in range(2):
            with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as reopened:
                status = reopened.source_status(a)
                self.assertEqual(status["observed_bytes"], 440)
                self.assertEqual(status["evicted_bytes"], 220)
                self.assertEqual(reopened.storage_usage()["raw_bytes"], 0)

    def test_prepare_starts_only_after_owned_raw_slot_is_free(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=5) as store:
            for _ in range(5):
                store.append(a, b"A" * 220)
            real_prepare = segment_module._prepare_linux_segment
            counts = []

            def bounded_prepare(root, seq):
                visible = {(path.stat().st_dev, path.stat().st_ino)
                           for path in self.root.glob("seg-*.raw")}
                visible.update((path.stat().st_dev, path.stat().st_ino)
                               for path in self.root.glob(".quarantine-*.raw"))
                anonymous = set()
                for entry in Path("/proc/self/fd").iterdir():
                    try:
                        target = os.readlink(entry)
                        info = os.stat(entry)
                    except OSError:
                        continue
                    if target.startswith(str(self.root) + "/") and info.st_nlink == 0:
                        anonymous.add((info.st_dev, info.st_ino))
                counts.append(len(visible | anonymous))
                self.assertLessEqual(counts[-1], 4)
                fd, identity = real_prepare(root, seq)
                self.assertLessEqual(len(visible | anonymous | {(identity.st_dev, identity.st_ino)}), 5)
                return fd, identity

            with patch.object(segment_module, "_prepare_linux_segment", side_effect=bounded_prepare):
                result = store.append(a, b"C" * 220)
            self.assertIsNone(result["storage_error"])
            self.assertTrue(counts)

    def test_unsupported_capture_keeps_original_victim_bytes(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, b"A" * 220)
            victim = next(self.root.glob("seg-*.raw"))
            before = victim.read_bytes()
            with patch.object(segment_module, "_capture_no_replace",
                              side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
                failed = store.append(a, b"B" * 220)
            self.assertEqual(failed["stored_bytes"], 0)
            self.assertIsNotNone(failed["storage_error"])
            self.assertEqual(victim.read_bytes(), before)
            self.assertEqual(list(self.root.glob(".quarantine-*.raw")), [])

    def test_capture_collision_keeps_both_entries(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, b"A" * 220)
            victim = next(self.root.glob("seg-*.raw"))
            before = victim.read_bytes()
            real_capture = segment_module._capture_no_replace
            blocker = b"different unknown raw bytes"

            def collide(root, public, quarantine):
                (self.root / quarantine).write_bytes(blocker)
                (self.root / quarantine).chmod(0o600)
                real_capture(root, public, quarantine)

            with patch.object(segment_module, "_capture_no_replace", side_effect=collide):
                failed = store.append(a, b"B" * 220)
            self.assertEqual(failed["stored_bytes"], 0)
            self.assertIn("FileExistsError:17", failed["storage_error"])
            self.assertEqual(victim.read_bytes(), before)
            quarantine, = self.root.glob(".quarantine-*.raw")
            self.assertEqual(quarantine.read_bytes(), blocker)

    def test_capture_directory_fsync_failure_preserves_quarantined_bytes(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, b"A" * 220)
            victim = next(self.root.glob("seg-*.raw"))
            before = victim.read_bytes()
            real_fsync = os.fsync

            def fail_after_capture(fd):
                if (os.fstat(fd).st_ino == os.fstat(store._root).st_ino
                        and list(self.root.glob(".quarantine-*.raw"))):
                    raise OSError(errno.EIO, "fixture directory fsync failure")
                real_fsync(fd)

            with patch.object(segment_module.os, "fsync", side_effect=fail_after_capture):
                failed = store.append(a, b"B" * 220)
            self.assertEqual(failed["stored_bytes"], 0)
            self.assertIsNotNone(failed["storage_error"])
            quarantine, = self.root.glob(".quarantine-*.raw")
            self.assertEqual(quarantine.read_bytes(), before)

    def test_single_segment_reopens_after_unlink_before_atomic_publish(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, b"A" * 220)
            with patch.object(segment_module, "_publish_prepared", side_effect=OSError(5, "fixture process stop")):
                failed = store.append(a, b"B" * 100)
            self.assertIsNotNone(failed["storage_error"])
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as reopened:
            self.assertEqual(reopened.source_status(a)["observed_bytes"], 320)
            self.assertEqual(reopened.read_raw(a, 0, 220)["missing"][0]["reason"], "evicted")

    def test_unnamed_header_crash_leaves_no_visible_partial_segment(self):
        program = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore, segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=2)
original = segments._write_all
def die_in_unnamed_header(fd, data):
    raw = bytes(data)
    if len(raw) == 16 and raw.startswith(b'WBRAW2\\0\\0'):
        os.write(fd, raw[:int(sys.argv[3])])
        os._exit(71)
    original(fd, data)
segments._write_all = die_in_unnamed_header
store.append(json.loads(sys.argv[2]), b'next' * 25, source_offset=0)
raise SystemExit(8)
"""
        for prefix in (0, 7, 15):
            with self.subTest(prefix=prefix):
                root = Path(self.temp.name) / f"unnamed-{prefix}"
                evicted, retained, incoming = source(), source(), source()
                with SegmentedRawStore(root, segment_limit=512, segment_count=2) as store:
                    store.append(evicted, b"A" * 220)
                    store.append(retained, b"B" * 220)
                before = {path.name: sha256(path.read_bytes()).hexdigest()
                          for path in root.glob("seg-*.raw")}
                child = self._child(program, str(root), json.dumps(incoming), str(prefix))
                self.assertEqual(child.returncode, 71, child.stderr)
                after = {path.name: sha256(path.read_bytes()).hexdigest()
                         for path in root.glob("seg-*.raw")}
                self.assertEqual(len(after), 1)
                self.assertEqual(after, {name: digest for name, digest in before.items()
                                         if name in after})
                with SegmentedRawStore(root, segment_limit=512, segment_count=2) as recovered:
                    self.assertEqual(recovered.read_raw(evicted, 0, 220)["missing"][0]["reason"],
                                     "evicted")
                    self.assertEqual(recovered.read_raw(retained, 0, 220)["status"], "available")
                    self.assertEqual(recovered.source_status(incoming)["observed_bytes"], 100)
                    self.assertEqual(recovered.read_raw(incoming, 0, 100)["missing"][0]["reason"],
                                     "storage_gap")

    def test_process_loss_after_private_prepare_before_identity_record(self):
        evicted, retained, incoming = source(), source(), source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as store:
            store.append(evicted, b"A" * 220)
            store.append(retained, b"B" * 220)
        before = {path.name: sha256(path.read_bytes()).hexdigest()
                  for path in self.root.glob("seg-*.raw")}
        program = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore, segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=2)
original = segments._prepare_linux_segment
def die_after_private_prepare(root, seq):
    original(root, seq)
    os._exit(75)
segments._prepare_linux_segment = die_after_private_prepare
store.append(json.loads(sys.argv[2]), b'next' * 25, source_offset=0)
raise SystemExit(8)
"""
        child = self._child(program, str(self.root), json.dumps(incoming))
        self.assertEqual(child.returncode, 75, child.stderr)
        after = {path.name: sha256(path.read_bytes()).hexdigest()
                 for path in self.root.glob("seg-*.raw")}
        self.assertEqual(len(after), 1)
        self.assertEqual(after, {name: digest for name, digest in before.items()
                                 if name in after})
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as recovered:
            self.assertEqual(recovered.read_raw(evicted, 0, 220)["missing"][0]["reason"],
                             "evicted")
            self.assertEqual(recovered.read_raw(retained, 0, 220)["status"], "available")
            self.assertEqual(recovered.source_status(incoming)["observed_bytes"], 100)
            self.assertEqual(recovered.read_raw(incoming, 0, 100)["missing"][0]["reason"],
                             "storage_gap")

    def test_process_loss_after_atomic_publish_recovers_authorized_full_header(self):
        retained, incoming = source(), source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(retained, b"A" * 220)
        program = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore, segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=1)
original = segments._publish_prepared
def die_after_publish(fd, root, name):
    original(fd, root, name)
    os._exit(72)
segments._publish_prepared = die_after_publish
store.append(json.loads(sys.argv[2]), b'next', source_offset=0)
raise SystemExit(8)
"""
        child = self._child(program, str(self.root), json.dumps(incoming))
        self.assertEqual(child.returncode, 72, child.stderr)
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as recovered:
            self.assertEqual(recovered.segment_sizes(), [16])
            self.assertEqual(recovered.read_raw(retained, 0, 220)["missing"][0]["reason"], "evicted")
            self.assertEqual(recovered.read_raw(incoming, 0, 4)["missing"][0]["reason"], "storage_gap")

    def test_process_loss_before_atomic_publish_leaves_no_unknown_path(self):
        retained, incoming = source(), source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(retained, b"A" * 220)
        program = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore, segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=1)
def die_before_publish(fd, root, name):
    os._exit(73)
segments._publish_prepared = die_before_publish
store.append(json.loads(sys.argv[2]), b'next', source_offset=0)
raise SystemExit(8)
"""
        child = self._child(program, str(self.root), json.dumps(incoming))
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual(list(self.root.glob("seg-*.raw")), [])
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as recovered:
            self.assertEqual(recovered.source_status(incoming)["observed_bytes"], 4)
            self.assertEqual(recovered.read_raw(incoming, 0, 4)["missing"][0]["reason"], "storage_gap")
            self.assertEqual(recovered.read_raw(retained, 0, 220)["missing"][0]["reason"], "evicted")

    def test_published_header_replacement_fails_without_deleting_other_inode(self):
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(source(), b"A" * 220)
        program = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore, segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=1)
original = segments._publish_prepared
def die_after_publish(fd, root, name):
    original(fd, root, name)
    os._exit(74)
segments._publish_prepared = die_after_publish
store.append(json.loads(sys.argv[2]), b'next', source_offset=0)
raise SystemExit(8)
"""
        child = self._child(program, str(self.root), json.dumps(source()))
        self.assertEqual(child.returncode, 74, child.stderr)
        published, = self.root.glob("seg-*.raw")
        header = published.read_bytes()
        self.assertEqual(len(header), 16)
        published.rename(Path(self.temp.name) / "held-original-header")
        published.write_bytes(header)
        published.chmod(0o600)
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(self.root, segment_limit=512, segment_count=1)
        self.assertEqual(published.read_bytes(), header)

    def test_published_name_replacement_during_pinned_scan_fails_closed(self):
        retained = source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as store:
            store.append(source(), b"A" * 220)
            store.append(retained, b"B" * 220)
        program = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore, segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=2)
original = segments._publish_prepared
def die_after_publish(fd, root, name):
    original(fd, root, name)
    os._exit(76)
segments._publish_prepared = die_after_publish
store.append(json.loads(sys.argv[2]), b'next', source_offset=0)
raise SystemExit(8)
"""
        child = self._child(program, str(self.root), json.dumps(source()))
        self.assertEqual(child.returncode, 76, child.stderr)
        paths = sorted(self.root.glob("seg-*.raw"))
        self.assertEqual(len(paths), 2)
        prior_digest = sha256(paths[0].read_bytes()).hexdigest()
        published = paths[-1]
        identity = (published.stat().st_dev, published.stat().st_ino)
        header = published.read_bytes()
        self.assertEqual(len(header), 16)
        real_read = os.read
        header_reads = 0

        def replace_during_scan(fd, count):
            nonlocal header_reads
            data = real_read(fd, count)
            info = os.fstat(fd)
            if count == 16 and (info.st_dev, info.st_ino) == identity:
                header_reads += 1
                if header_reads == 2:
                    published.rename(Path(self.temp.name) / "held-pinned-header")
                    published.write_bytes(header)
                    published.chmod(0o600)
            return data

        with patch.object(segment_module.os, "read", side_effect=replace_during_scan):
            with self.assertRaises(SegmentIntegrityError):
                SegmentedRawStore(self.root, segment_limit=512, segment_count=2)
        self.assertEqual(header_reads, 2)
        self.assertEqual(published.read_bytes(), header)
        self.assertEqual(sha256(paths[0].read_bytes()).hexdigest(), prior_digest)

    @staticmethod
    def _child(program: str, *args: str) -> subprocess.CompletedProcess[str]:
        project = Path(__file__).resolve().parents[2]
        return subprocess.run([sys.executable, "-c", program, *args], cwd=project,
                              env={"PYTHONPATH": str(project / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
                              capture_output=True, text=True, timeout=10, check=False)

    def test_preexisting_partial_next_segment_is_not_deleted_as_crash_recovery(self):
        a, b, c = source(), source(), source()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as store:
            store.append(a, b"A" * 220)
            store.append(b, b"B" * 220)
            originals = {path.name: sha256(path.read_bytes()).hexdigest()
                         for path in self.root.glob("seg-*.raw")}
            preexisting = self.root / f"seg-{store._control['next']:016d}.raw"
            preexisting.write_bytes(b"WBRAW2")
            preexisting.chmod(0o600)
            failed = store.append(c, b"C" * 10)
            self.assertEqual(failed["stored_bytes"], 0)
            self.assertEqual(failed["missing_bytes"], 10)
            self.assertIsNotNone(failed["storage_error"])
            self.assertEqual(preexisting.read_bytes(), b"WBRAW2")
            self.assertEqual({path.name: sha256(path.read_bytes()).hexdigest()
                              for path in self.root.glob("seg-*.raw") if path != preexisting}, originals)
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(self.root, segment_limit=512, segment_count=2)
        self.assertEqual(preexisting.read_bytes(), b"WBRAW2")

    def test_recovery_does_not_delete_replaced_partial_header(self):
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as store:
            seq = store._control["next"]
            partial = self.root / f"seg-{seq:016d}.raw"
            partial.write_bytes(b"WBRAW2")
            partial.chmod(0o600)
            identity = partial.stat()
            store._control["creating"] = {"seq": seq, "dev": identity.st_dev, "ino": identity.st_ino}
            store._save()
            partial.rename(Path(self.temp.name) / "held-partial")
            partial.write_bytes(b"WBRAW2")
            partial.chmod(0o600)
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(self.root, segment_limit=512, segment_count=2)
        self.assertEqual(partial.read_bytes(), b"WBRAW2")

    def test_authenticated_partial_cleanup_is_idempotent_after_unlink(self):
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as store:
            seq = store._control["next"]
            partial = self.root / f"seg-{seq:016d}.raw"
            partial.write_bytes(b"WBRAW2")
            partial.chmod(0o600)
            identity = partial.stat()
            store._control["creating"] = {"seq": seq, "dev": identity.st_dev, "ino": identity.st_ino}
            store._save()
            partial.unlink()
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=2) as reopened:
            self.assertIsNone(reopened._control["creating"])
            self.assertEqual(reopened.segment_sizes(), [])

    def test_complete_checksum_tamper_is_not_truncated_as_partial_tail(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            store.append(a, b"abc")
            segment = self.root / f"seg-{store._control['active']:016d}.raw"
        before_size = segment.stat().st_size
        with segment.open("r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            stream.write(b"x")
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(self.root, segment_limit=512)
        self.assertEqual(segment.stat().st_size, before_size)

    def test_unexpected_active_segment_removal_is_not_labeled_rotation(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            store.append(a, b"abc")
            segment = self.root / f"seg-{store._control['active']:016d}.raw"
        segment.unlink()
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(self.root, segment_limit=512)

    def test_unowned_or_linked_paths_fail_closed(self):
        self.root.mkdir(mode=0o700)
        (self.root / "alien").write_bytes(b"keep")
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(self.root)
        self.assertEqual((self.root / "alien").read_bytes(), b"keep")
        (self.root / "alien").unlink()
        with SegmentedRawStore(self.root) as store:
            pass
        target = Path(self.temp.name) / "target"
        target.write_bytes(b"untouched")
        os.link(target, self.root / "seg-0000000000000001.raw")
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(self.root)
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_replaced_root_is_reported_without_writing_replacement(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            store.append(a, b"old")
            detached = Path(self.temp.name) / "detached"
            self.root.rename(detached)
            replacement = Path(self.temp.name) / "replacement"
            replacement.mkdir(mode=0o700)
            sentinel = replacement / "sentinel"
            sentinel.write_bytes(b"keep")
            self.root.symlink_to(replacement, target_is_directory=True)
            failed = store.append(a, b"new")
            self.assertEqual(failed["stored_bytes"], 0)
            self.assertEqual(failed["missing_bytes"], 3)
            self.assertTrue(failed["observation_continues"])
            self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_replaced_singleton_lock_does_not_admit_a_second_writer(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            store.append(a, b"old")
            lock = self.root / ".segment.lock"
            lock.rename(self.root / "held-lock")
            lock.write_bytes(b"replacement")
            lock.chmod(0o600)
            failed = store.append(a, b"new")
            self.assertEqual(failed["stored_bytes"], 0)
            self.assertEqual(failed["missing_bytes"], 3)
            self.assertTrue(failed["observation_continues"])

    def test_legacy_bytes_unchanged_and_proof_before_fault(self):
        legacy_root = Path(self.temp.name) / "legacy"
        with RawLogStore(legacy_root, per_run_limit=32, project_limit=32) as old:
            old.append("run-one", b"legacy-bytes")
        old_digest = sha256((legacy_root / "run-one.log").read_bytes()).hexdigest()
        a = source()
        proof = []
        model_calls = []
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            proof.append({"observed": b"evidence", "source": a})
            with patch.object(store, "_append_frame", side_effect=OSError(28, "full")):
                result = store.append(a, b"evidence")
            self.assertEqual(result["missing_bytes"], 8)
            self.assertEqual(len(proof), 1)
            self.assertEqual(model_calls, [])
            self.assertTrue(result["observation_continues"])
        self.assertEqual(sha256((legacy_root / "run-one.log").read_bytes()).hexdigest(), old_digest)

    def test_control_write_failure_returns_loss_and_refuses_later_writes(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            with patch.object(store, "_save", side_effect=OSError(28, "full")):
                failed = store.append(a, b"first")
            self.assertEqual(failed["missing_bytes"], 5)
            self.assertTrue(failed["observation_continues"])
            later = store.append(a, b"next")
            self.assertEqual(later["stored_bytes"], 0)
            self.assertEqual(later["storage_error"], "raw_store_uncertain")

    def test_unsupported_unnamed_inode_reports_loss_without_unsafe_fallback(self):
        a = source()
        with SegmentedRawStore(self.root, segment_limit=512) as store:
            with patch.object(segment_module, "_prepare_linux_segment",
                              side_effect=OSError(errno.EOPNOTSUPP, "fixture unsupported")):
                result = store.append(a, b"observed", source_offset=0)
            self.assertEqual((result["stored_bytes"], result["missing_bytes"]), (0, 8))
            self.assertTrue(result["observation_continues"])
            self.assertIn("OSError", result["storage_error"])
            self.assertEqual(store.segment_sizes(), [])
            self.assertEqual(store.read_raw(a, 0, 8)["missing"][0]["reason"], "storage_gap")

    def test_full_catalog_implicit_failure_remains_unknown_after_restart(self):
        a, overflow = source(), source()
        with patch.object(segment_module, "MAX_SOURCES", 1):
            with SegmentedRawStore(self.root, segment_limit=512) as store:
                self.assertEqual(store.append(a, b"kept")["stored_bytes"], 4)
                first = store.append(overflow, b"abc")
                second = store.append(overflow, b"def")
                self.assertEqual((first["start"], first["end"],
                                  second["start"], second["end"]), (None, None, None, None))
                self.assertEqual((first["missing_bytes"], second["missing_bytes"]), (3, 3))
                self.assertEqual(store.read_raw(overflow, 0, 6)["status"], "unknown")
            with SegmentedRawStore(self.root, segment_limit=512) as reopened:
                later = reopened.append(overflow, b"ghi")
                self.assertEqual((later["start"], later["end"]), (None, None))
                self.assertEqual(reopened.source_status(overflow)["status"], "unknown")

    def test_pre_store_proof_survives_eviction_without_model_calls(self):
        a = source()
        observed = b"condition-marker"
        proof = {"source": a, "matched": b"marker" in observed,
                 "digest": sha256(observed).hexdigest()}
        model_calls: list[object] = []
        with SegmentedRawStore(self.root, segment_limit=512, segment_count=1) as store:
            store.append(a, observed)
            for _ in range(3):
                store.append(a, b"x" * 220)
            raw = store.read_raw(a, 0, len(observed))
            self.assertEqual(raw["status"], "unavailable")
            self.assertEqual(raw["missing"][0]["reason"], "evicted")
        self.assertTrue(proof["matched"])
        self.assertEqual(proof["digest"], sha256(observed).hexdigest())
        self.assertEqual(model_calls, [])


class DurableProofSurvivalTests(unittest.TestCase):
    """The existing durable observation seam remains independent of v2 eviction."""

    def setUp(self):
        import_module("tests.workflow.test_run").WorkflowRuntimeTests.setUp(self)

    def approved_task(self, command):
        return import_module("tests.workflow.test_run").WorkflowRuntimeTests.approved_task(self, command)

    def test_recorded_condition_proof_remains_after_same_source_eviction(self):
        task_id = self.approved_task("printf 'PASS\\n'; printf PASS > outcome.txt")
        automation = import_module("tests.workflow.test_run").AUTOMATION
        run = self.workflow.start(task_id, 1, worktree_path=self.root / "proof-run",
                                  artifacts_root=self.artifacts, automation=automation)
        self.addCleanup(run.close)
        self.assertTrue(run.collect(timeout=8)["exit_confirmed"])
        before = self.repository.get_condition_proofs(run.run_id)
        self.assertEqual(len(before), 1)
        self.assertTrue(before[0]["matched"])
        with SegmentedRawStore(self.root / "v2", segment_limit=512, segment_count=1) as store:
            source_ref = run._source_ref
            store.append(source_ref, b"PASS")
            for _ in range(3):
                store.append(source_ref, b"x" * 220)
            self.assertEqual(store.read_raw(source_ref, 0, 4)["missing"][0]["reason"], "evicted")
        after = self.repository.get_condition_proofs(run.run_id)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
