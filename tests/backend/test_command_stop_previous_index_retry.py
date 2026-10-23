"""C-T2 (delta re-review P3): the previous-backend command index remembers a failed journal read.

Seam: a real ``Backend`` with fake OMPs through ``_tool_request`` (the seam of ``PreviousBackendIndexTests`` in
``test_command_stop_hardening``), with the journal's ``read_all`` counting reads and failing with ``OSError(ENOSPC)``.

Expected values are this file's own: the number of journal reads the test counts, the errno it injects, the clock
it advances by hand (the 30 s interval is spelled out here, not read from the product), the ids it sends. The
process is one the test started and kills on cleanup.
"""
from __future__ import annotations

import errno
import subprocess
from uuid import uuid4

import test_command_stop_authority as authority


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class PreviousIndexRetryTests(authority.BackendStopFixture):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.backend._previous_index_clock = self.clock
        self.reads, self.failing = [], True
        journal_file = self.backend.handoffs._journal
        read_all = journal_file.read_all

        def read_all_or_fail():
            self.reads.append("read_all")
            if self.failing:
                raise OSError(errno.ENOSPC, "no space left on device")
            return read_all()
        journal_file.read_all = read_all_or_fail
        self.addCleanup(lambda: setattr(journal_file, "read_all", read_all))

    def previous_command(self, seconds):
        process = subprocess.Popen(["sleep", seconds], start_new_session=True)
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait()))
        command_id = str(uuid4())
        self.backend.handoffs.record({"type": "terminal_started", "command_id": command_id, "task_id": None,
                                      "command": "sleep " + seconds, "cwd": "/tmp",
                                      "started_at": "2026-10-20T10:00:00Z", "log_path": "/tmp/none.log"})
        return command_id, process

    def assert_unknown(self, command_id):
        for result in (self.manager_stop(command_id), self.worker_terminal(command=None, stop=command_id)):
            self.assertEqual((result["status"], result["reason"]), ("not_found", "unknown_command"), result)

    def test_a_failed_read_is_not_repeated_within_the_interval(self):
        previous, process = self.previous_command("613.5")
        self.assert_unknown(previous)
        self.assertEqual(len(self.reads), 1, self.reads)
        for number in range(6):
            self.clock.now += 4.0  # 24 s in all: still inside the 30 s interval
            self.assert_unknown(previous if number % 2 else str(uuid4()))
        self.assertEqual(len(self.reads), 1, self.reads)
        self.assertIsNone(process.poll(), "the process was not signalled")

    def test_after_the_interval_the_next_lookup_reads_again_and_a_success_is_kept(self):
        previous, process = self.previous_command("613.4")
        self.assert_unknown(previous)
        self.clock.now += 31.0
        self.failing = False
        for result in (self.manager_stop(previous), self.worker_terminal(command=None, stop=previous)):
            self.assertEqual((result["status"], result["reason"]), ("refused", "previous_backend_command"), result)
        self.assertEqual(len(self.reads), 2, self.reads)
        self.clock.now += 300.0
        self.assertEqual(self.manager_stop(str(uuid4()))["reason"], "unknown_command")
        self.assertEqual(len(self.reads), 2, self.reads)  # the built index is kept, never re-read
        self.assertIsNone(process.poll(), "the process was not signalled")

    def test_a_second_failure_waits_a_fresh_interval(self):
        self.assert_unknown(str(uuid4()))
        self.clock.now += 31.0
        self.assert_unknown(str(uuid4()))  # retried, fails again
        self.assertEqual(len(self.reads), 2, self.reads)
        self.clock.now += 20.0
        self.assert_unknown(str(uuid4()))
        self.assertEqual(len(self.reads), 2, self.reads)
