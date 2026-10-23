"""Review P3 follow-ups of the command-stop work.

B1 a registration that fails after the service stored a live entry leaves no live entry and is not stoppable: a later
   stop answers ``refused: registration_failed`` with no signal (seam: the real ``CommandStopService`` whose
   ``register`` raises after the base class stored its entry, the ``fail_register`` shape of ``CapturingStop``, driven
   through ``register_target`` and ``stop``).
B2 the previous-backend index error raised inside the retry window carries the original journal read error as its
   ``__cause__`` (seam: ``Backend._journaled_unfinished_command`` directly, the narrowest seam: its callers swallow
   the raised error, so only a direct call can observe it).

Expected values are this file's own: the errno it injects, the clock it advances by hand, the strings it compares.
"""
from __future__ import annotations

import errno
import unittest
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, StopTarget
from workbench.backend.command_stop_owner import register_target

import test_command_stop_previous_index_retry as retry


class RegisterThenFail(CommandStopService):
    """Raises after the base class stored the live entry: the fault inside ``register`` after its assignment."""

    def register(self, target):
        super().register(target)
        raise RuntimeError("fault after the live entry was stored")


class FailedRegistrationLeavesNoLiveEntryTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.stop = RegisterThenFail(journal=self.records.append)
        self.addCleanup(self.stop.close)
        self.command_id = str(uuid4())
        self.assertFalse(register_target(self.stop, StopTarget(self.command_id, "worker", "terminal", None,
                                                               identity=lambda: None)))

    def test_no_live_entry_remains(self):
        self.assertNotIn(self.command_id, self.stop._live)

    def test_a_later_stop_is_refused_as_registration_failed_without_a_signal(self):
        for role in ("manager", "worker"):
            answer = self.stop.stop(role, self.command_id)
            self.assertEqual((answer["status"], answer["reason"]), ("refused", "registration_failed"), answer)
            self.assertNotIn("signals", answer)
        self.assertEqual(self.stop.stop_marker(self.command_id), {})


class PreviousIndexErrorCauseTests(retry.PreviousIndexRetryFixture):
    def test_a_lookup_inside_the_window_chains_the_original_read_error(self):
        with self.assertRaises(OSError) as first:
            self.backend._journaled_unfinished_command(str(uuid4()))
        original = first.exception
        self.assertEqual(original.errno, errno.ENOSPC)
        self.clock.now += 5.0
        with self.assertRaises(OSError) as inside:
            self.backend._journaled_unfinished_command(str(uuid4()))
        self.assertIs(inside.exception.__cause__, original)
        self.assertEqual(len(self.reads), 1, "no second journal read inside the window")
        self.clock.now += 20.0  # still inside 30 s from the failure
        with self.assertRaises(OSError) as again:
            self.backend._journaled_unfinished_command(str(uuid4()))
        self.assertIs(again.exception.__cause__, original)

    def test_just_below_30_s_no_read_and_the_error_is_chained(self):
        with self.assertRaises(OSError) as first:
            self.backend._journaled_unfinished_command(str(uuid4()))
        self.clock.now += 29.9  # the 30 s interval is spelled out here, not read from the product
        with self.assertRaises(OSError) as inside:
            self.backend._journaled_unfinished_command(str(uuid4()))
        self.assertIs(inside.exception.__cause__, first.exception)
        self.assertEqual(len(self.reads), 1, "no second journal read at 29.9 s")

    def test_at_exactly_30_s_the_journal_is_read_again(self):
        # the product compares ``clock < retry_at`` to refuse, so the question at exactly +30.0 s reads again
        with self.assertRaises(OSError):
            self.backend._journaled_unfinished_command(str(uuid4()))
        self.clock.now += 30.0
        with self.assertRaises(OSError) as again:
            self.backend._journaled_unfinished_command(str(uuid4()))
        self.assertEqual(len(self.reads), 2, "the journal is read again at exactly 30 s")
        self.assertEqual(again.exception.errno, errno.ENOSPC, "a fresh read error, not the retry-window error")
        self.assertIsNone(again.exception.__cause__)

    def test_just_after_30_s_the_journal_is_read_again_and_a_success_is_kept(self):
        with self.assertRaises(OSError):
            self.backend._journaled_unfinished_command(str(uuid4()))
        self.clock.now += 30.1
        self.failing = False
        self.assertFalse(self.backend._journaled_unfinished_command(str(uuid4())))
        self.assertEqual(len(self.reads), 2, self.reads)


if __name__ == "__main__":
    unittest.main()
