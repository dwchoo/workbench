"""Actual store/flock and fixed-root callback checks; synthetic policy evidence only."""
import base64
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tests.backend import test_raw_rotation_independent as fixtures
from workbench.backend.summary_scheduler import SummarySourceCollector
from workbench.storage.log_raw import port as raw_port
from workbench.storage.log_raw.port import DisabledSegmentedRawPort, command_source
from workbench.storage.log_raw.segments import SegmentedRawStore, ReadonlySegmentedRawStore


class ObservedLeafLock:
    """Keep the real leaf RLock; events show attempts before blocking, not timing guesses."""
    def __init__(self):
        self.lock = threading.RLock()
        self.attempted = {name: threading.Event() for name in ("reader", "writer")}
        self.entered = {name: threading.Event() for name in ("reader", "writer")}

    def __enter__(self):
        name = threading.current_thread().name
        if name in self.attempted:
            self.attempted[name].set()
        self.lock.acquire()
        if name in self.entered:
            self.entered[name].set()
        return self

    def __exit__(self, *_):
        self.lock.release()


class RestartRawReadIndependentTests(unittest.TestCase):
    def previous(self, root, identity=None, body=b"previous output"):
        identity = identity or str(uuid4())
        folder = root / "workflow" / "terminal"
        folder.mkdir(parents=True, mode=0o700); folder.parent.chmod(0o700)
        with SegmentedRawStore(root / "raw-v2") as store:
            port = DisabledSegmentedRawPort(store)
            _, descriptor = port.prepare_command(folder, identity)
            port.append(descriptor, body)
        span = {"schemaVersion": 1, "source": command_source(identity), "start": 0,
                "end": len(body), "digest": sha256(body).hexdigest()}
        records = [{"source_id": str(uuid4()), "cursor": 1, "range": span}]
        return identity, body, records

    def assert_flock_conflict(self, root, operation):
        fd = os.open(root / "raw-v2" / ".segment.lock", os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_restart_collector_read_then_first_writer_waits_for_actual_shared_flock_close(self):
        self.run_interleaving(reader_first=True)

    def test_restart_first_writer_then_collector_rereads_published_singleton_under_leaf_lock(self):
        self.run_interleaving(reader_first=False)

    def run_interleaving(self, *, reader_first):
        fixture, reference, _ = fixtures.validator_fixture(self)
        backend = fixtures.backend_fixture(self, fixture)
        identity, body, records = self.previous(fixture.root)
        descriptor = fixture.root / "workflow" / "terminal" / (identity + ".json")
        before = descriptor.read_bytes()
        backend.raw_rotation.activate(reference)
        leaf = ObservedLeafLock()
        backend._raw_port_lock = leaf
        holding, release = threading.Event(), threading.Event()
        outputs, errors, stores_read = {}, [], []
        original_read = raw_port.read_source
        def read_source(store, *args, **kwargs):
            stores_read.append(store)
            if reader_first:
                self.assertIsInstance(store, ReadonlySegmentedRawStore)
                self.assert_flock_conflict(fixture.root, fcntl.LOCK_EX)
                holding.set()
                if not release.wait(5): raise TimeoutError("owned reader release")
            return original_read(store, *args, **kwargs)
        def writer_factory(root):
            store = SegmentedRawStore(root)
            self.addCleanup(store.close)
            if not reader_first:
                self.assert_flock_conflict(fixture.root, fcntl.LOCK_SH)
                holding.set()
                if not release.wait(5): raise TimeoutError("owned writer publication release")
            return store
        collector = SummarySourceCollector(fixture.root, lambda: object(),
                                           raw_query_port=backend._raw_source_query)
        def read():
            try: outputs["read"] = collector.collect(None, {"kind": "command", "id": identity}, records)
            except BaseException as exc: errors.append(exc)
        def write():
            try: outputs["writer"] = backend._new_raw_port()
            except BaseException as exc: errors.append(exc)
        threads = {"reader": threading.Thread(target=read, name="reader"),
                   "writer": threading.Thread(target=write, name="writer")}
        first, second = ("reader", "writer") if reader_first else ("writer", "reader")
        with patch.object(raw_port, "read_source", read_source), \
             patch("workbench.backend.service.SegmentedRawStore", writer_factory):
            try:
                threads[first].start()
                self.assertTrue(holding.wait(5), "real store has acquired its flock")
                threads[second].start()
                self.assertTrue(leaf.attempted[second].wait(5), "second caller reached leaf lock")
                self.assertFalse(leaf.entered[second].is_set(), "held leaf serializes both orderings")
                self.assertIsNone(backend.segmented_raw_port, "singleton publication still pending")
            finally:
                release.set()
                for thread in threads.values():
                    if thread.ident is not None: thread.join(5)
                self.assertFalse(any(thread.is_alive() for thread in threads.values()))
        self.assertEqual(errors, [])
        pieces, gaps = outputs["read"]
        self.assertEqual(gaps, [])
        self.assertEqual([p.text for p in pieces], [body.decode()])
        self.assertEqual([p.range for p in pieces], [records[0]["range"]])
        self.assertIs(outputs["writer"], backend.segmented_raw_port)
        if not reader_first:
            self.assertIs(stores_read[0], backend.segmented_raw_port.store)
            self.assertNotIsInstance(stores_read[0], ReadonlySegmentedRawStore)
        self.assert_flock_conflict(fixture.root, fcntl.LOCK_SH)
        self.assertEqual(descriptor.read_bytes(), before)
        exact = backend._raw_source_query("command", identity, offset=0, limit=len(body))
        self.assertIs(type(exact), dict)
        self.assertEqual(exact, backend.raw_query("command", identity, 0, len(body)))
        self.assertEqual(backend.raw_status()["segmented_v1"]["state"], "available")

    def test_fixed_root_exact_result_and_callback_absent_equivalence_preserve_exceptions(self):
        fixture, _, _ = fixtures.validator_fixture(self)
        backend = fixtures.backend_fixture(self, fixture)
        identity, body, records = self.previous(fixture.root)
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other)
            self.previous(other_root, identity, b"wrong root body")
            stale_port = Mock()
            stale_port.read.side_effect = AssertionError("callback must win over captured port")
            with_callback = SummarySourceCollector(other_root, lambda: object(), segmented=stale_port,
                                                   raw_query_port=backend._raw_source_query)
            original_route = SummarySourceCollector(fixture.root, lambda: object())
            expected = original_route.collect(None, {"kind": "command", "id": identity}, records)
            self.assertEqual(with_callback.collect(None, {"kind": "command", "id": identity}, records), expected)
            stale_port.read.assert_not_called()
            response = backend._raw_source_query("command", identity, offset=0, limit=len(body))
            self.assertIs(type(response), dict)
            self.assertEqual(response, raw_port.raw_query(fixture.root, "command", identity,
                                                         offset=0, limit=len(body)))
            self.assertEqual(base64.b64decode(response["read"]["chunks"][0]["dataBase64"]), body)
        missing = str(uuid4())
        with self.assertRaises(FileNotFoundError):
            backend._raw_source_query("command", missing, offset=0, limit=1)
        self.assertEqual(backend.raw_query("command", missing, 0, 1),
                         {"status": "unavailable", "reason": "FileNotFoundError"})
        path = fixture.root / "workflow" / "terminal" / (identity + ".json")
        path.write_text("{}"); path.chmod(0o600)
        with self.assertRaises(ValueError):
            backend._raw_source_query("command", identity, offset=0, limit=len(body))
        with self.assertRaises(ValueError):
            with_callback.collect(None, {"kind": "command", "id": identity}, records)


if __name__ == "__main__":
    unittest.main()
