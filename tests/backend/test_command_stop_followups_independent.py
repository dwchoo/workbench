"""Independent verification of the command-stop follow-ups (C1 end time of ``already_exited``, C2 repeated cancel).

Expected values come from the follow-up contract, not from the implementation or its own tests:

C1. A stop of a command that ended before any signal is ``already_exited`` and its ``ended_at`` is when the OWNER
    reported the end, never when the stop was asked; repeats return the same value; a command stopped mid-episode is
    ``exited`` (the end time of that is not this contract's business).
C2. A cancel of an already closed Task is still ``rejected: task_closed`` (``closed_reason`` unchanged) and now also
    carries the first cancel's ``host_commands`` fields (same item shape; ``[]`` when none;
    ``host_commands_unavailable: true`` when the provider fails or is missing; ``host_command_note`` exactly when the
    list is not empty). A non-cancel message to a closed Task gains none of it; an unknown Task gains none of it.

Seams: ``CommandStopService`` (registry only, plus a real ``TerminalService`` over a real host shell for the
mid-episode / self-ended cases) and ``TaskFlow`` through ``HandoffService`` (``to_worker`` as the Manager), with
fake ``host_commands`` providers or the real ``TerminalService.commands_for_task``. Time is read from a fake counter
clock (one tick per ``_now()`` call) so "the end" and "the request" can never coincide by chance; the expected end
time is bracketed by the ticks the test itself observed around the owner's report.

Every process is one the test started inside its own host shell; fixtures close the pane (and its group) on teardown.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from workbench.backend.command_stop import CommandStopService, StopPolicy, StopTarget

import test_cancel_host_command as cancel_base
import test_command_stop_escalation as escalation
from test_flow_terminal import wait_until
from test_task_flow import FlowFixture

HOST_ITEM_KEYS = {"command_id", "kind", "owner", "state", "started_at", "log_path", "stop"}


class CounterClock:
    """``_now()`` replacement: every call returns the next second of a fixed day, so each call is distinguishable."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ticks = 0

    def __call__(self):
        with self.lock:
            self.ticks += 1
            return self.format(self.ticks)

    @staticmethod
    def format(tick):
        return "2026-10-01T%02d:%02d:%02dZ" % (tick // 3600, tick // 60 % 60, tick % 60)

    def window(self, call, *args):
        """Run ``call`` and return (result, first tick it could have used, last tick it could have used) as strings."""
        with self.lock:
            low = self.ticks + 1
        result = call(*args)
        with self.lock:
            high = self.ticks
        return result, self.format(low), self.format(high)


class RegistryClockCase(unittest.TestCase):
    POLICY = StopPolicy(identity_wait=0.4, sigint_wait=0.5, sigterm_wait=0.5, sigkill_wait=0.5, call_wait=5.0,
                        poll_interval=0.02)
    EXITED = {"status": "exited", "exit_code": 5, "signal": None}

    def setUp(self):
        self.clock = CounterClock()
        patcher = mock.patch("workbench.backend.command_stop._now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.records = []
        self.service = CommandStopService(journal=self.records.append, policy=self.POLICY)
        self.addCleanup(self.service.close)

    def register(self, owner="worker"):
        command_id = str(uuid4())
        self.service.register(StopTarget(command_id, owner, "terminal", "T-ind", identity=lambda: None))
        return command_id

    def end(self, command_id, result=None):
        """The owner reports the end; returns the (first, last) clock tick that report could have stamped."""
        _, low, high = self.clock.window(self.service.command_ended, command_id, result or self.EXITED)
        return low, high

    def assert_between(self, value, low, high, message=""):
        self.assertTrue(low <= value <= high, f"{value} not within [{low}, {high}] {message}")


class AlreadyExitedEndTimeTests(RegistryClockCase):
    def test_ended_before_the_request_reports_the_owner_report_time_not_the_request_time(self):
        command_id = self.register()
        low, high = self.end(command_id)
        for _ in range(7):  # time passes (and other clock readers run) before anybody asks
            self.clock()
        result, asked_low, _ = self.clock.window(self.service.stop, "worker", command_id)
        self.assertEqual(result["status"], "already_exited", result)
        self.assert_between(result["ended_at"], low, high, "the owner's report")
        self.assertLess(result["ended_at"], asked_low, "not the time of the request")

    def test_repeated_stops_return_the_same_end_time_even_when_the_clock_moves_on(self):
        command_id = self.register()
        self.end(command_id)
        seen = []
        for _ in range(4):
            seen.append(self.service.stop("worker", command_id))
            for _ in range(3):
                self.clock()
        self.assertEqual({r["ended_at"] for r in seen}, {seen[0]["ended_at"]}, seen)
        self.assertEqual([r["status"] for r in seen], ["already_exited"] * 4, seen)
        self.assertEqual([bool(r.get("repeat")) for r in seen], [False, True, True, True], "repeats as before")

    def test_each_command_keeps_its_own_end_time(self):
        first, second = self.register(), self.register()
        first_low, first_high = self.end(first)
        for _ in range(5):
            self.clock()
        second_low, second_high = self.end(second)
        one = self.service.stop("worker", first)
        two = self.service.stop("worker", second)
        self.assert_between(one["ended_at"], first_low, first_high)
        self.assert_between(two["ended_at"], second_low, second_high)
        self.assertLess(one["ended_at"], two["ended_at"])

    def test_the_end_arriving_while_the_stop_waits_reports_the_end_not_the_moment_it_was_noticed(self):
        command_id = self.register()
        stamp = {}

        def owner_reports():
            time.sleep(0.15)
            stamp["window"] = self.end(command_id)
            for _ in range(11):  # the clock keeps moving between the report and the stop call's return
                self.clock()

        timer = threading.Thread(target=owner_reports)
        timer.start()
        result = self.service.stop("worker", command_id)
        timer.join(5)
        self.assertEqual(result["status"], "already_exited", result)
        self.assert_between(result["ended_at"], *stamp["window"], "the owner's report")
        again = self.service.stop("worker", command_id)
        self.assertEqual(again["ended_at"], result["ended_at"])

    def test_an_id_without_a_live_owner_but_a_recorded_end_reports_the_recorded_end(self):
        command_id = str(uuid4())  # never registered: the owner only reports the end (a record without an entry)
        low, high = self.end(command_id)
        for _ in range(6):
            self.clock()
        result = self.service.stop("worker", command_id)
        self.assertEqual(result["status"], "already_exited", result)
        self.assert_between(result["ended_at"], low, high)
        self.assertEqual(self.service.stop("worker", command_id)["ended_at"], result["ended_at"])

    def test_the_manager_asking_for_a_workers_ended_command_gets_the_same_end_time(self):
        command_id = self.register("worker")
        low, high = self.end(command_id)
        for _ in range(4):
            self.clock()
        manager = self.service.stop("manager", command_id)
        worker = self.service.stop("worker", command_id)
        self.assertEqual((manager["status"], worker["status"]), ("already_exited", "already_exited"))
        self.assert_between(manager["ended_at"], low, high)
        self.assertEqual(manager["ended_at"], worker["ended_at"])

    def test_the_other_fields_of_already_exited_are_unchanged(self):
        command_id = self.register()
        self.end(command_id, {"status": "exited", "exit_code": 3, "signal": None})
        result = self.service.stop("worker", command_id)
        self.assertEqual((result["status"], result["exit_code"], result["signal"], result["command_id"]),
                         ("already_exited", 3, None, command_id), result)
        self.assertFalse(result.get("signals"), result)

    def test_a_declared_unknown_is_still_never_already_exited(self):
        command_id = self.register()
        self.end(command_id, {"status": "unknown", "reason": "host shell state unknown: x"})
        result = self.service.stop("worker", command_id)
        self.assertEqual(result["status"], "unknown", result)


class RealCommandEndTimeTests(escalation.EscalationFixture):
    """A real command in a real host shell; the clock is the real one (second resolution), so time must really pass."""

    @staticmethod
    def stamp(seconds_from_now=0.0):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds_from_now))

    def test_a_command_that_finished_by_itself_reports_when_it_finished_not_when_the_stop_came(self):
        low = self.stamp()
        done = self.run_command("echo finished-by-itself; exit 3", wait=30)
        self.assertEqual((done["status"], done["exit_code"]), ("exited", 3), done)
        high = self.stamp(1.0)  # the owner's report is at most this late (the call has returned)
        time.sleep(2.3)  # the stop request comes clearly later (second resolution)
        asked = self.stamp()
        result = self.stop(done["command_id"])
        self.assertEqual(result["status"], "already_exited", result)
        self.assertTrue(low <= result["ended_at"] <= high, (low, result["ended_at"], high))
        self.assertLess(result["ended_at"], asked, "not the request time")
        time.sleep(1.2)
        again = self.stop(done["command_id"])
        self.assertEqual(again["ended_at"], result["ended_at"])

    def test_a_command_stopped_mid_episode_is_exited_and_not_already_exited(self):
        started = self.run_command("sleep 650.3")
        self.assertEqual(started["status"], "running", started)
        self.kill_at_cleanup(*escalation.pids_running("sleep", "650.3"))
        result = self.stop(started["command_id"])
        self.assertEqual(result["status"], "exited", result)
        self.assertTrue(result["signals"], "it was signalled")
        again = self.stop(started["command_id"])
        self.assertEqual((again["status"], again.get("repeat")), ("exited", True), again)


# -- C2 --------------------------------------------------------------------------------------------------------------


def item(command_id, state="running", owner="worker"):
    return {"command_id": command_id, "kind": "terminal", "owner": owner, "state": state,
            "started_at": "2026-10-01T00:00:00Z", "log_path": f"/nonexistent/{command_id}.log", "stop": None}


HOST_FIELDS = ("host_commands", "host_commands_unavailable", "host_command_note")


class RepeatedCancelProviderTests(FlowFixture):
    """Fake ``host_commands`` providers: a repeated cancel carries exactly what the first cancel carried."""

    def closed_work(self, provider):
        work = self.running_work()
        self.flow.host_commands = provider
        return work["task_id"]

    def cancel_args(self, task_id):
        return {"kind": "work", "message": "cancel", "cancel": True, "task_id": task_id}

    def host_fields(self, result):
        return {key: result[key] for key in HOST_FIELDS if key in result}

    def assert_parity(self, provider):
        task_id = self.closed_work(provider)
        first = self.to_worker(self.cancel_args(task_id))
        self.assertEqual(first["status"], "cancelled", first)
        again = self.to_worker(self.cancel_args(task_id))
        self.assertEqual((again["status"], again["reason"], again["closed_reason"]),
                         ("rejected", "task_closed", "cancelled"), again)
        self.assertEqual(self.host_fields(again), self.host_fields(first))
        return first, again

    def test_running_commands_are_listed_with_the_first_cancels_shape_and_a_note(self):
        items = [item("c-1"), item("c-2", state="stopping")]
        first, again = self.assert_parity(lambda task_id: [dict(i) for i in items])
        self.assertEqual([i["command_id"] for i in again["host_commands"]], ["c-1", "c-2"])
        for entry in again["host_commands"]:
            self.assertEqual(set(entry), HOST_ITEM_KEYS)
        self.assertTrue(again["host_command_note"])
        self.assertNotIn("host_commands_unavailable", again)

    def test_an_empty_list_is_empty_without_marker_or_note(self):
        _, again = self.assert_parity(lambda task_id: [])
        self.assertEqual(again["host_commands"], [])
        self.assertNotIn("host_commands_unavailable", again)
        self.assertNotIn("host_command_note", again)

    def test_a_failing_provider_is_unavailable_without_a_note(self):
        def failing(task_id):
            raise RuntimeError("provider down")
        _, again = self.assert_parity(failing)
        self.assertEqual(again["host_commands"], [])
        self.assertIs(again["host_commands_unavailable"], True)
        self.assertNotIn("host_command_note", again)

    def test_no_provider_is_unavailable_like_the_first_cancel(self):
        _, again = self.assert_parity(None)
        self.assertEqual(again["host_commands"], [])
        self.assertIs(again["host_commands_unavailable"], True)

    def test_the_commands_listed_are_those_of_the_closed_task_not_of_the_current_one(self):
        asked = []
        by_task = {}

        def provider(task_id):
            asked.append(task_id)
            return [dict(i) for i in by_task.get(task_id, [])]
        old = self.closed_work(provider)
        by_task[old] = [item("old-command")]
        self.assertEqual(self.to_worker(self.cancel_args(old))["status"], "cancelled")
        newer = self.new_work("the next job")
        self.assertEqual(newer["status"], "dispatched", newer)
        by_task[newer["task_id"]] = [item("new-command")]
        asked.clear()
        again = self.to_worker(self.cancel_args(old))
        self.assertEqual((again["status"], again["reason"]), ("rejected", "task_closed"), again)
        self.assertEqual([i["command_id"] for i in again["host_commands"]], ["old-command"])
        self.assertEqual(asked, [old], "the provider was asked for the closed Task only")

    def test_a_task_closed_for_another_reason_keeps_its_closed_reason_and_lists_commands(self):
        task_id = self.finished_experiment()
        self.flow.host_commands = lambda tid: [item("leftover")]
        superseded = self.new_work("a newer job ends the finished experiment")
        self.assertEqual(superseded["status"], "dispatched", superseded)
        again = self.to_worker({"kind": "experiment", "message": "cancel", "cancel": True, "task_id": task_id})
        self.assertEqual((again["status"], again["reason"]), ("rejected", "task_closed"), again)
        self.assertEqual(again["closed_reason"], "superseded_by_new_task", again)
        self.assertEqual([i["command_id"] for i in again["host_commands"]], ["leftover"])
        self.assertTrue(again["host_command_note"])

    def test_a_follow_up_to_a_closed_task_gains_none_of_the_fields(self):
        task_id = self.closed_work(lambda tid: [item("c-1")])
        self.assertEqual(self.to_worker(self.cancel_args(task_id))["status"], "cancelled")
        for cancel in (None, False):
            args = {"kind": "work", "message": "more work", "task_id": task_id}
            if cancel is not None:
                args["cancel"] = cancel
            result = self.to_worker(args)
            self.assertEqual((result["status"], result["reason"], result["closed_reason"]),
                             ("rejected", "task_closed", "cancelled"), result)
            for key in HOST_FIELDS:
                self.assertNotIn(key, result)

    def test_a_cancel_of_an_unknown_or_missing_task_gains_none_of_the_fields(self):
        self.running_work()
        self.flow.host_commands = lambda tid: [item("c-1")]
        unknown = self.to_worker({"kind": "work", "message": "cancel", "cancel": True, "task_id": str(uuid4())})
        self.assertEqual(unknown["status"], "rejected", unknown)
        self.assertEqual(unknown["reason"], "unknown_task", unknown)
        missing = self.to_worker({"kind": "work", "message": "cancel", "cancel": True})
        self.assertEqual(missing["status"], "rejected", missing)
        for result in (unknown, missing):
            for key in HOST_FIELDS:
                self.assertNotIn(key, result)

    def test_the_first_cancel_is_unchanged_by_the_follow_up(self):
        task_id = self.closed_work(lambda tid: [item("c-1")])
        first = self.to_worker(self.cancel_args(task_id))
        self.assertEqual(first["status"], "cancelled", first)
        self.assertEqual([i["command_id"] for i in first["host_commands"]], ["c-1"])
        self.assertTrue(first["host_command_note"])
        self.assertNotIn("reason", first)


class RepeatedCancelRealCommandTests(cancel_base.CancelHostCommandFixture):
    """The real ``TerminalService`` provider: the repeated cancel lists what truly still runs."""

    def test_a_running_command_is_listed_by_the_repeated_cancel_and_untouched(self):
        work = self.running_work()
        started = self.run_command("sleep 702.1")
        self.assertEqual(started["status"], "running", started)
        self.assertEqual(self.cancel(work["task_id"])["status"], "cancelled")
        for _ in range(2):
            again = self.cancel(work["task_id"])
            self.assertEqual((again["status"], again["reason"]), ("rejected", "task_closed"), again)
            (entry,) = again["host_commands"]
            self.assertEqual(set(entry), HOST_ITEM_KEYS)
            self.assertEqual((entry["command_id"], entry["owner"], entry["kind"], entry["state"]),
                             (started["command_id"], "worker", "terminal", "running"))
            self.assertTrue(again["host_command_note"])
        self.assertTrue(self.terminal.current()["running"])
        self.assertEqual(self.stop(started["command_id"])["status"], "exited")
        after = self.cancel(work["task_id"])
        self.assertEqual((after["status"], after["reason"]), ("rejected", "task_closed"), after)
        self.assertEqual(after["host_commands"], [])
        self.assertNotIn("host_command_note", after)
        self.assertNotIn("host_commands_unavailable", after)

    def test_a_command_being_stopped_is_listed_as_stopping_with_its_stop_info(self):
        work = self.running_work()
        started = self.run_command("bash -c 'trap \"\" INT; sleep 702.2'")
        self.assertEqual(started["status"], "running", started)
        self.assertEqual(self.cancel(work["task_id"])["status"], "cancelled")
        results = []
        stopper = threading.Thread(target=lambda: results.append(self.stop(started["command_id"])), daemon=True)
        stopper.start()
        try:
            self.assertTrue(wait_until(lambda: any(
                i["state"] == "stopping" for i in self.cancel(work["task_id"]).get("host_commands", [])), 5))
            again = self.cancel(work["task_id"])
            (entry,) = again["host_commands"]
            self.assertEqual(set(entry), HOST_ITEM_KEYS)
            self.assertEqual(entry["state"], "stopping")
            self.assertIsNotNone(entry["stop"], entry)
        finally:
            stopper.join(15)
        self.assertEqual(results[0]["status"], "exited", results)


if __name__ == "__main__":
    unittest.main()
