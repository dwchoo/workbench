"""Independent, owned-fixture checks of the disabled segmented raw engine."""

from __future__ import annotations

import base64
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from workbench.storage.log_raw import (
    DEFAULT_PER_RUN_LIMIT,
    DEFAULT_PROJECT_LIMIT,
    RawLogStore,
    SegmentIntegrityError,
    SegmentedRawStore,
)
from workbench.storage.log_raw import segments as segment_module


TIME = "2026-01-02T03:04:05Z"
HEADER_CRASH_PROGRAM = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore
from workbench.storage.log_raw import segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=2)
original = segments._write_all
def die_before_header(fd, data):
    raw = bytes(data)
    if len(raw) == 16 and raw.startswith(b'WBRAW2\\0\\0'):
        os.write(fd, raw[:int(sys.argv[3])])
        os._exit(68)
    original(fd, data)
segments._write_all = die_before_header
store.append(json.loads(sys.argv[2]), b'next' * 25,
             observed_at='2026-01-02T03:04:05Z', source_offset=0)
raise SystemExit(8)
"""
PUBLISH_CRASH_PROGRAM = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore, segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=2)
original = segments._publish_prepared
def die_at_publish(fd, root, name):
    if sys.argv[3] == 'before':
        os._exit(72)
    original(fd, root, name)
    os._exit(73)
segments._publish_prepared = die_at_publish
store.append(json.loads(sys.argv[2]), b'next' * 25, source_offset=0)
raise SystemExit(8)
"""
EVICTION_CRASH_PROGRAM = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore
from workbench.storage.log_raw import segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=1)
phase = sys.argv[3]
original_save = store._save
def crash_at_durable_phase():
    original_save()
    pending = store._control['eviction']
    if pending is not None and pending['phase'] == phase:
        os._exit({'planned': 79, 'armed': 80}[phase])
store._save = crash_at_durable_phase
if phase == 'unlink':
    original_unlink = segments.os.unlink
    def crash_after_unlink(path, *args, **kwargs):
        original_unlink(path, *args, **kwargs)
        if str(path).startswith('.quarantine-'):
            os._exit(81)
    segments.os.unlink = crash_after_unlink
if phase == 'accounting':
    original_finish = store._finish_eviction
    def crash_after_accounting(seqs):
        result = original_finish(seqs)
        os._exit(82)
    store._finish_eviction = crash_after_accounting
store.append(json.loads(sys.argv[2]), b'B' * 220, source_offset=220)
raise SystemExit(8)
"""


def source() -> dict[str, object]:
    return {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
            "commandId": str(uuid4())}


class IndependentRawSegmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owned = tempfile.TemporaryDirectory(prefix="dvl11-independent-", dir="/dev/shm")
        self.addCleanup(self.owned.cleanup)
        self.base = Path(self.owned.name)

    def _child(self, program: str, *args: str) -> subprocess.CompletedProcess[str]:
        project = Path(__file__).resolve().parents[2]
        env = {"PYTHONPATH": str(project / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [sys.executable, "-c", program, *args], cwd=project, env=env,
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_default_legacy_limits_and_bytes_survive_explicit_v2_fixture(self) -> None:
        self.assertEqual(DEFAULT_PER_RUN_LIMIT, 64 * 1024 * 1024)
        self.assertEqual(DEFAULT_PROJECT_LIMIT, 512 * 1024 * 1024)
        legacy = self.base / "legacy"
        v2 = self.base / "v2"
        with RawLogStore(legacy) as old:
            old.append("one", b"legacy-only\x00bytes")
        legacy_file = legacy / "one.log"
        digest = sha256(legacy_file.read_bytes()).hexdigest()
        self.assertFalse(v2.exists())
        with SegmentedRawStore(v2, segment_limit=512, segment_count=1) as new:
            result = new.append(source(), b"v2-only", observed_at=TIME)
            self.assertEqual(result["stored_bytes"], 7)
        self.assertEqual(sha256(legacy_file.read_bytes()).hexdigest(), digest)
        self.assertEqual(legacy_file.read_bytes(), b"legacy-only\x00bytes")

    def test_exact_physical_boundary_includes_file_header_and_frame(self) -> None:
        ref = source()
        payload = b"A" * 100
        with SegmentedRawStore(self.base / "measure", segment_limit=2048,
                               segment_count=2) as measured:
            measured.append(ref, payload, observed_at=TIME)
            exact_limit, = measured.segment_sizes()
        self.assertGreaterEqual(exact_limit, 256)
        with SegmentedRawStore(self.base / "exact", segment_limit=exact_limit,
                               segment_count=2) as store:
            first = store.append(ref, payload, observed_at=TIME, source_offset=0)
            self.assertEqual((first["stored_bytes"], first["missing_bytes"]), (100, 0))
            self.assertEqual(store.segment_sizes(), [exact_limit])
            second = store.append(ref, b"B", observed_at=TIME, source_offset=100)
            self.assertEqual((second["stored_bytes"], second["missing_bytes"]), (1, 0))
            self.assertEqual(len(store.segment_sizes()), 2)
            self.assertEqual(store.read_raw(ref, 0, 101)["status"], "available")
            self.assertTrue(all(size <= exact_limit for size in store.segment_sizes()))

    def test_another_process_cannot_acquire_writer_lease(self) -> None:
        root = self.base / "lease"
        attempt = """
import sys
from workbench.storage.log_raw import SegmentedRawStore, SegmentIntegrityError
try:
    with SegmentedRawStore(sys.argv[1], segment_limit=512):
        raise SystemExit(9)
except SegmentIntegrityError:
    raise SystemExit(0)
"""
        with SegmentedRawStore(root, segment_limit=512) as store:
            store.append(source(), b"owner", observed_at=TIME)
            child = self._child(attempt, str(root))
            self.assertEqual(child.returncode, 0, child.stderr)
        with SegmentedRawStore(root, segment_limit=512) as successor:
            self.assertEqual(len(successor.segment_sizes()), 1)

    def test_process_loss_during_payload_recovers_only_complete_frames(self) -> None:
        root = self.base / "crash"
        ref = source()
        with SegmentedRawStore(root, segment_limit=512) as store:
            store.append(ref, b"ok", observed_at=TIME)
        interrupted = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore
from workbench.storage.log_raw import segments
ref = json.loads(sys.argv[2])
store = SegmentedRawStore(sys.argv[1], segment_limit=512)
original = segments._write_all
def torn(fd, data):
    if bytes(data) == b'lost-frame':
        os.write(fd, b'lost')
        os._exit(47)
    original(fd, data)
segments._write_all = torn
store.append(ref, b'lost-frame', observed_at='2026-01-02T03:04:05Z', source_offset=2)
raise SystemExit(8)
"""
        child = self._child(interrupted, str(root), json.dumps(ref))
        self.assertEqual(child.returncode, 47, child.stderr)
        with SegmentedRawStore(root, segment_limit=512) as recovered:
            page = recovered.read_raw(ref, 0, 12)
            self.assertEqual(page["status"], "partial")
            self.assertEqual([(part["range"]["start"], part["range"]["end"])
                              for part in page["chunks"]], [(0, 2)])
            self.assertEqual([(gap["start"], gap["end"], gap["reason"])
                              for gap in page["missing"]], [(2, 12, "storage_gap")])
            resumed = recovered.append(ref, b"next", observed_at=TIME, source_offset=12)
            self.assertEqual((resumed["start"], resumed["end"]), (12, 16))

    def test_unnamed_header_process_loss_preserves_survivor_and_records_gaps(self) -> None:
        for prefix_bytes in (0, 7, 15):
            with self.subTest(header_bytes=prefix_bytes):
                root = self.base / f"header-{prefix_bytes}"
                first, retained, interrupted_source = source(), source(), source()
                with SegmentedRawStore(root, segment_limit=512, segment_count=2) as store:
                    self.assertEqual(store.append(first, b"A" * 220)["stored_bytes"], 220)
                    self.assertEqual(store.append(retained, b"B" * 220)["stored_bytes"], 220)
                    self.assertEqual(len(store.segment_sizes()), 2)
                before = {path.name: sha256(path.read_bytes()).hexdigest()
                          for path in root.glob("seg-*.raw")}
                child = self._child(HEADER_CRASH_PROGRAM, str(root), json.dumps(interrupted_source),
                                    str(prefix_bytes))
                self.assertEqual(child.returncode, 68, child.stderr)
                after = {path.name: sha256(path.read_bytes()).hexdigest()
                         for path in root.glob("seg-*.raw")}
                self.assertEqual(len(after), 1)
                self.assertEqual(after, {name: digest for name, digest in before.items()
                                         if name in after})
                with SegmentedRawStore(root, segment_limit=512, segment_count=2) as recovered:
                    self.assertEqual([(gap["start"], gap["end"], gap["reason"])
                                      for gap in recovered.read_raw(first, 0, 220)["missing"]],
                                     [(0, 220, "evicted")])
                    prior = recovered.read_raw(retained, 0, 220)
                    self.assertEqual(prior["status"], "available")
                    self.assertEqual(b"".join(base64.b64decode(part["dataBase64"])
                                              for part in prior["chunks"]), b"B" * 220)
                    self.assertEqual(recovered.source_status(interrupted_source)["observed_bytes"],
                                     100)
                    missing = recovered.read_raw(interrupted_source, 0, 100)
                    self.assertEqual(missing["status"], "unavailable")
                    self.assertEqual([(gap["start"], gap["end"], gap["reason"])
                                      for gap in missing["missing"]], [(0, 100, "storage_gap")])
                    resumed = recovered.append(interrupted_source, b"!", source_offset=100)
                    self.assertEqual((resumed["start"], resumed["end"], resumed["stored_bytes"]),
                                     (100, 101, 1))

    def test_private_prepare_then_process_loss_before_identity_publication_recovers(self) -> None:
        root = self.base / "owned-create-crash"
        evicted = source()
        retained = source()
        incoming = source()
        with SegmentedRawStore(root, segment_limit=512, segment_count=2) as store:
            store.append(evicted, b"A" * 220)
            store.append(retained, b"B" * 220)
        before = {path.name: sha256(path.read_bytes()).hexdigest()
                  for path in root.glob("seg-*.raw")}
        interrupted = """
import json, os, sys
from workbench.storage.log_raw import SegmentedRawStore
from workbench.storage.log_raw import segments
store = SegmentedRawStore(sys.argv[1], segment_limit=512, segment_count=2)
original = segments._prepare_linux_segment
def die_after_private_prepare(root_fd, seq):
    original(root_fd, seq)
    os._exit(69)
segments._prepare_linux_segment = die_after_private_prepare
store.append(json.loads(sys.argv[2]), b'next' * 25, source_offset=0)
raise SystemExit(8)
"""
        child = self._child(interrupted, str(root), json.dumps(incoming))
        self.assertEqual(child.returncode, 69, child.stderr)
        segment_paths = sorted(root.glob("seg-*.raw"))
        self.assertEqual(len(segment_paths), 1)
        self.assertEqual({path.name: sha256(path.read_bytes()).hexdigest()
                          for path in segment_paths},
                         {name: digest for name, digest in before.items()
                          if name in {path.name for path in segment_paths}})
        prior_digest = sha256(segment_paths[0].read_bytes()).hexdigest()
        with SegmentedRawStore(root, segment_limit=512, segment_count=2) as recovered:
            self.assertEqual([(gap["start"], gap["end"], gap["reason"])
                              for gap in recovered.read_raw(evicted, 0, 220)["missing"]],
                             [(0, 220, "evicted")])
            prior = recovered.read_raw(retained, 0, 220)
            self.assertEqual(prior["status"], "available")
            self.assertEqual(b"".join(base64.b64decode(part["dataBase64"])
                                      for part in prior["chunks"]), b"B" * 220)
            self.assertEqual(recovered.source_status(incoming)["observed_bytes"], 100)
            gap = recovered.read_raw(incoming, 0, 100)
            self.assertEqual(gap["status"], "unavailable")
            self.assertEqual(gap["missing"][0]["reason"], "storage_gap")
        self.assertEqual(sha256(segment_paths[0].read_bytes()).hexdigest(), prior_digest)

    def test_real_process_death_at_eviction_boundaries_is_accounted_once(self) -> None:
        for phase, exit_code in (("planned", 79), ("armed", 80),
                                 ("unlink", 81), ("accounting", 82)):
            with self.subTest(phase=phase):
                root = self.base / f"eviction-{phase}"
                ref = source()
                with SegmentedRawStore(root, segment_limit=512, segment_count=1) as store:
                    self.assertEqual(store.append(ref, b"A" * 220)["stored_bytes"], 220)
                child = self._child(EVICTION_CRASH_PROGRAM, str(root), json.dumps(ref), phase)
                self.assertEqual(child.returncode, exit_code, child.stderr)
                for _ in range(2):
                    with SegmentedRawStore(root, segment_limit=512, segment_count=1) as recovered:
                        status = recovered.source_status(ref)
                        self.assertEqual(status["observed_bytes"], 440)
                        self.assertEqual(status["evicted_bytes"], 220)
                        page = recovered.read_raw(ref, 0, 440)
                        self.assertEqual(page["chunks"], [])
                        self.assertEqual([(gap["start"], gap["end"], gap["reason"])
                                          for gap in page["missing"]],
                                         [(0, 220, "evicted"), (220, 440, "storage_gap")])
                        self.assertLessEqual(len(list(root.glob("seg-*.raw"))
                                                 + list(root.glob(".quarantine-*.raw"))), 1)

    def test_public_name_swap_during_active_write_cannot_report_stored_bytes(self) -> None:
        root = self.base / "active-write-swap"
        donor_root = self.base / "active-write-donor"
        ref = source()
        with SegmentedRawStore(donor_root, segment_limit=1024) as donor_store:
            self.assertEqual(donor_store.append(ref, b"A" * 8, observed_at=TIME)["stored_bytes"], 8)
        donor, = donor_root.glob("seg-*.raw")
        donor_bytes = donor.read_bytes()
        with SegmentedRawStore(root, segment_limit=1024) as store:
            self.assertEqual(store.append(ref, b"A" * 8, observed_at=TIME)["stored_bytes"], 8)
            public, = root.glob("seg-*.raw")
            held = self.base / "held-active-write"
            identity = (public.stat().st_dev, public.stat().st_ino)
            old_size = public.stat().st_size
            real_fsync = os.fsync
            swapped = False

            def swap_after_frame_fsync(fd):
                nonlocal swapped
                real_fsync(fd)
                info = os.fstat(fd)
                if (not swapped and (info.st_dev, info.st_ino) == identity
                        and info.st_size > old_size):
                    public.rename(held)
                    donor.rename(public)
                    swapped = True

            with patch.object(segment_module.os, "fsync", side_effect=swap_after_frame_fsync):
                receipt = store.append(ref, b"B" * 8, observed_at=TIME, source_offset=8)
            self.assertTrue(swapped)
            self.assertEqual(public.read_bytes(), donor_bytes)
            self.assertIn(b"B" * 8, held.read_bytes())
            self.assertEqual((receipt["start"], receipt["end"]), (8, 16))
            self.assertEqual((receipt["stored_bytes"], receipt["missing_bytes"]), (0, 8))
            self.assertIsNotNone(receipt["storage_error"])
            self.assertTrue(receipt["observation_continues"])

    def test_death_before_and_after_atomic_publication_preserves_prior_and_gap(self) -> None:
        for phase, exit_code in (("before", 72), ("after", 73)):
            with self.subTest(phase=phase):
                root = self.base / f"publish-{phase}"
                prior, incoming = source(), source()
                with SegmentedRawStore(root, segment_limit=512, segment_count=2) as store:
                    store.append(source(), b"A" * 220)
                    store.append(prior, b"B" * 220)
                child = self._child(PUBLISH_CRASH_PROGRAM, str(root), json.dumps(incoming), phase)
                self.assertEqual(child.returncode, exit_code, child.stderr)
                paths = sorted(root.glob("seg-*.raw"))
                self.assertEqual(len(paths), 1 if phase == "before" else 2)
                if phase == "after":
                    self.assertEqual(paths[-1].stat().st_size, 16)
                with SegmentedRawStore(root, segment_limit=512, segment_count=2) as recovered:
                    page = recovered.read_raw(prior, 0, 220)
                    self.assertEqual(page["status"], "available")
                    self.assertEqual(b"".join(base64.b64decode(part["dataBase64"])
                                              for part in page["chunks"]), b"B" * 220)
                    self.assertEqual(recovered.source_status(incoming)["observed_bytes"], 100)
                    loss = recovered.read_raw(incoming, 0, 100)
                    self.assertEqual(loss["status"], "unavailable")
                    self.assertEqual([(gap["start"], gap["end"], gap["reason"])
                                      for gap in loss["missing"]], [(0, 100, "storage_gap")])

    def test_arbitrary_partial_future_file_without_identity_is_not_repaired(self) -> None:
        root = self.base / "arbitrary-partial"
        with SegmentedRawStore(root, segment_limit=512, segment_count=2) as store:
            store.append(source(), b"prior")
            future = root / f"seg-{store._control['next']:016d}.raw"
            previous = sorted(root.glob("seg-*.raw"))[0]
            prior_digest = sha256(previous.read_bytes()).hexdigest()
        future.write_bytes(b"WBRAW2")
        future.chmod(0o600)
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(root, segment_limit=512, segment_count=2)
        self.assertEqual(future.read_bytes(), b"WBRAW2")
        self.assertEqual(sha256(previous.read_bytes()).hexdigest(), prior_digest)

    def test_published_authenticated_header_replaced_by_other_inode_fails_closed(self) -> None:
        root = self.base / "replaced-intent"
        with SegmentedRawStore(root, segment_limit=512, segment_count=2) as store:
            store.append(source(), b"A" * 220)
            store.append(source(), b"B" * 220)
        child = self._child(PUBLISH_CRASH_PROGRAM, str(root), json.dumps(source()), "after")
        self.assertEqual(child.returncode, 73, child.stderr)
        segment_paths = sorted(root.glob("seg-*.raw"))
        published = segment_paths[-1]
        original_identity = (published.stat().st_dev, published.stat().st_ino)
        content = published.read_bytes()
        self.assertEqual(len(content), 16)
        prior_digest = sha256(segment_paths[0].read_bytes()).hexdigest()
        published.rename(self.base / "detached-authenticated-header")
        published.write_bytes(content)
        published.chmod(0o600)
        self.assertNotEqual((published.stat().st_dev, published.stat().st_ino), original_identity)
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(root, segment_limit=512, segment_count=2)
        self.assertEqual(published.read_bytes(), content)
        self.assertEqual(sha256(segment_paths[0].read_bytes()).hexdigest(), prior_digest)

    def test_recovery_does_not_adopt_substitution_after_inode_verification(self) -> None:
        root = self.base / "verify-scan-race"
        donor = self.base / "verify-scan-donor"
        with SegmentedRawStore(root, segment_limit=512, segment_count=2) as store:
            store.append(source(), b"A" * 220)
            store.append(source(), b"B" * 220)
        child = self._child(PUBLISH_CRASH_PROGRAM, str(root), json.dumps(source()), "after")
        self.assertEqual(child.returncode, 73, child.stderr)
        segment_paths = sorted(root.glob("seg-*.raw"))
        published = segment_paths[-1]
        self.assertEqual(published.stat().st_size, 16)
        prior_digest = sha256(segment_paths[0].read_bytes()).hexdigest()
        forged = source()
        with SegmentedRawStore(donor, segment_limit=512, segment_count=3) as other:
            other.append(source(), b"A" * 220)
            other.append(source(), b"B" * 220)
            self.assertEqual(other.append(forged, b"forged")["stored_bytes"], 6)
            replacement_bytes = sorted(donor.glob("seg-*.raw"))[-1].read_bytes()
        self.assertEqual(len(sorted(donor.glob("seg-*.raw"))), 3)
        substituted = False
        real_owned_open = segment_module._owned_open

        def replace_before_scan(root_fd, name, flags, *, create=False):
            nonlocal substituted
            if name == published.name and flags == os.O_RDWR and not substituted:
                published.rename(self.base / "held-published-header")
                published.write_bytes(replacement_bytes)
                published.chmod(0o600)
                substituted = True
            return real_owned_open(root_fd, name, flags, create=create)

        failure = None
        adopted = None
        with patch.object(segment_module, "_owned_open", side_effect=replace_before_scan):
            try:
                with SegmentedRawStore(root, segment_limit=512, segment_count=2) as recovered:
                    adopted = recovered.read_raw(forged, 0, 6)
            except (SegmentIntegrityError, OSError) as exc:
                failure = exc
        if substituted:
            self.assertIsNotNone(failure, f"unverified substitute result: {adopted}")
            self.assertEqual(published.read_bytes(), replacement_bytes)
        else:
            self.assertIsNone(failure)
        self.assertEqual(sha256(segment_paths[0].read_bytes()).hexdigest(), prior_digest)

    def test_complete_future_segment_without_intent_is_not_adopted(self) -> None:
        root = self.base / "future-file"
        donor = self.base / "donor"
        prior_ref, forged_ref = source(), source()
        with SegmentedRawStore(root, segment_limit=512, segment_count=2) as store:
            store.append(prior_ref, b"prior")
            future = root / f"seg-{store._control['next']:016d}.raw"
            previous = sorted(root.glob("seg-*.raw"))[0]
            prior_digest = sha256(previous.read_bytes()).hexdigest()
        with SegmentedRawStore(donor, segment_limit=512, segment_count=2) as other:
            other.append(source(), b"A" * 220)
            self.assertEqual(other.append(forged_ref, b"forged")["stored_bytes"], 6)
            valid_future = sorted(donor.glob("seg-*.raw"))[-1]
            self.assertEqual(valid_future.name, future.name)
            future.write_bytes(valid_future.read_bytes())
            future.chmod(0o600)
        forged_digest = sha256(future.read_bytes()).hexdigest()
        with self.assertRaises(SegmentIntegrityError):
            SegmentedRawStore(root, segment_limit=512, segment_count=2)
        self.assertEqual(sha256(future.read_bytes()).hexdigest(), forged_digest)
        self.assertEqual(sha256(previous.read_bytes()).hexdigest(), prior_digest)

    def test_source_catalog_capacity_returns_loss_without_escaping_observer(self) -> None:
        root = self.base / "source-capacity"
        incoming = source()
        observed = b"approved-condition-marker"
        observation = {"matched": b"condition" in observed,
                       "digest": sha256(observed).hexdigest(), "source": incoming}
        with SegmentedRawStore(root, segment_limit=1_000_000) as store:
            first = source()
            self.assertEqual(store.append(first, b"a", source_offset=0)["stored_bytes"], 1)
            for _ in range(1023):
                self.assertEqual(store.append(source(), b"a", source_offset=0)["stored_bytes"], 1)
            result = store.append(incoming, observed, source_offset=0)
            self.assertEqual((result["start"], result["end"]), (0, len(observed)))
            self.assertEqual((result["stored_bytes"], result["missing_bytes"]),
                             (0, len(observed)))
            self.assertIsNotNone(result["storage_error"])
            self.assertTrue(result["observation_continues"])
            self.assertEqual(store.source_status(first)["retained_bytes"], 1)
            self.assertLessEqual(store.storage_usage()["metadata_bytes"], 1_000_000)
            unknown = store.read_raw(incoming, 0, len(observed))
            self.assertEqual(unknown["status"], "unknown")
            self.assertEqual(unknown["missing"][0]["reason"], "unknown")
        self.assertTrue(observation["matched"])
        self.assertEqual(observation["digest"], sha256(observed).hexdigest())

    def test_full_catalog_repeated_implicit_offset_never_fabricates_same_range(self) -> None:
        incoming = source()
        caller_ranged = source()
        root = self.base / "quota-implicit"
        with SegmentedRawStore(root, segment_limit=1_000_000) as store:
            for _ in range(1024):
                self.assertEqual(store.append(source(), b"x")["stored_bytes"], 1)
            first = store.append(incoming, b"abc")
            second = store.append(incoming, b"def")
            for receipt in (first, second):
                self.assertEqual(receipt["stored_bytes"], 0)
                self.assertEqual(receipt["missing_bytes"], 3)
                self.assertIsNotNone(receipt["storage_error"])
                self.assertTrue(receipt["observation_continues"])
            if all(type(receipt.get("start")) is int and type(receipt.get("end")) is int
                   for receipt in (first, second)):
                self.assertEqual((first["start"], first["end"], second["start"], second["end"]),
                                 (0, 3, 3, 6))
            else:
                self.assertTrue(all(receipt.get("start") is None and receipt.get("end") is None
                                    for receipt in (first, second)))
            self.assertEqual(store.read_raw(incoming, 0, 6)["status"], "unknown")
            explicit_first = store.append(caller_ranged, b"ghi", source_offset=0)
            explicit_second = store.append(caller_ranged, b"jkl", source_offset=3)
            self.assertEqual((explicit_first["start"], explicit_first["end"],
                              explicit_second["start"], explicit_second["end"]), (0, 3, 3, 6))
            self.assertEqual((explicit_first["missing_bytes"], explicit_second["missing_bytes"]),
                             (3, 3))
            self.assertEqual(store.source_status(caller_ranged)["status"], "unknown")
        with SegmentedRawStore(root, segment_limit=1_000_000) as reopened:
            for ref in (incoming, caller_ranged):
                result = reopened.read_raw(ref, 0, 6)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["chunks"], [])
                self.assertEqual([(gap["start"], gap["end"], gap["reason"])
                                  for gap in result["missing"]], [(0, 6, "unknown")])

    def test_unsupported_atomic_file_capability_is_storage_loss_without_fallback(self) -> None:
        for stage in ("tmpfile", "publish"):
            with self.subTest(stage=stage):
                root = self.base / f"unsupported-{stage}"
                ref = source()
                with SegmentedRawStore(root, segment_limit=512) as store:
                    if stage == "tmpfile":
                        real_open = segment_module.os.open

                        def unsupported_tmpfile(path, flags, *args, **kwargs):
                            if path == "." and flags & os.O_TMPFILE == os.O_TMPFILE:
                                raise OSError(errno.EOPNOTSUPP, "fixture lacks O_TMPFILE")
                            return real_open(path, flags, *args, **kwargs)

                        target = patch.object(segment_module.os, "open", side_effect=unsupported_tmpfile)
                    else:
                        target = patch.object(segment_module, "_publish_prepared",
                                              side_effect=OSError(errno.EOPNOTSUPP,
                                                                  "fixture lacks linkat"))
                    with target:
                        receipt = store.append(ref, b"observed", source_offset=0)
                    self.assertEqual((receipt["stored_bytes"], receipt["missing_bytes"]), (0, 8))
                    self.assertIsNotNone(receipt["storage_error"])
                    self.assertTrue(receipt["observation_continues"])
                    self.assertEqual(list(root.glob("seg-*.raw")), [])
                with SegmentedRawStore(root, segment_limit=512) as reopened:
                    self.assertEqual(reopened.source_status(ref)["observed_bytes"], 8)
                    loss = reopened.read_raw(ref, 0, 8)
                    self.assertEqual(loss["status"], "unavailable")
                    self.assertEqual(loss["missing"][0]["reason"], "storage_gap")

    def test_aliased_parent_is_rejected_without_touching_target(self) -> None:
        target = self.base / "target"
        target.mkdir(mode=0o700)
        sentinel = target / "keep"
        sentinel.write_bytes(b"outside")
        alias = self.base / "alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises((OSError, SegmentIntegrityError)):
            SegmentedRawStore(alias / "raw", segment_limit=512)
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertFalse((target / "raw").exists())


if __name__ == "__main__":
    unittest.main()
