"""Independent verification of the Task line stop label and host-command notes (ProductModel, public seam).

Seam: ``ProductModel.apply_snapshot`` with a snapshot ``task`` and ``status_lines()`` / ``task_text()``.
Expected values are derived from the contract, not from the implementation.
"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from support import FakeSender, snapshot  # noqa: E402
from wcwidth import wcswidth  # noqa: E402
from workbench.ui.product.model import ProductModel  # noqa: E402

RUNNING_NOTE = "명령 실행 중"
UNKNOWN_NOTE = "명령 종료 미확인"
UNAVAILABLE_NOTE = "명령 확인 불가"
ALL_NOTES = (RUNNING_NOTE, UNKNOWN_NOTE, UNAVAILABLE_NOTE)
COMMAND_ID = "cmd-ZZ-secret-id-0123"
LOG_PATH = "/var/tmp/secret-log-path/out.log"


def task_snapshot(**task):
    snap = snapshot()
    base = {"task_id": "t1", "kind": "experiment", "status": "running", "summary": "lr sweep", "since": 1.0,
            "active": True, "revision": 1, "run_id": "run-1", "runs_started": 1, "retry_limit": 3,
            "held_reason": None, "closed_reason": None, "cancel_requested": False, "last_result": None}
    base.update(task)
    snap["task"] = base
    return snap


def cmd(state, **extra):
    return {"command_id": COMMAND_ID, "kind": "terminal", "owner": "worker", "state": state,
            "started_at": "2026-01-01T00:00:00Z", "log_path": LOG_PATH, "stop": None, **extra}


def finished_stopped(stopped_by="__missing__", **extra):
    last = {"outcome": "stopped", "exit_code": None, "signal": "SIGTERM"}
    if stopped_by != "__missing__":
        last["stopped_by"] = stopped_by
    return {"status": "finished", "last_result": last, **extra}


class Base(unittest.TestCase):
    def render(self, cols=120, **task):
        model = ProductModel(FakeSender(), 30, cols, clock=lambda: 1000.0)
        model.apply_snapshot(task_snapshot(**task))
        return model

    def lines(self, cols=120, **task):
        return self.render(cols, **task).status_lines()

    def task_line(self, cols=120, **task):
        lines = self.lines(cols, **task)
        hits = [line for line in lines if "작업:" in line]
        self.assertEqual(len(hits), 1, lines)
        return hits[0]

    def assertClean(self, model_or_lines):
        lines = model_or_lines.status_lines() if hasattr(model_or_lines, "status_lines") else model_or_lines
        for line in lines:
            bad = [ch for ch in line if not ch.isprintable()]
            self.assertEqual(bad, [], repr(line))
            self.assertNotIn(COMMAND_ID, line)
            self.assertNotIn(LOG_PATH, line)
            self.assertNotIn("secret", line)

    def notes_in(self, line):
        return [note for note in ALL_NOTES if note in line]


class StoppedLabelTests(Base):
    def test_known_requesters(self):
        for who, label in (("user", "사용자"), ("manager", "Manager"), ("worker", "Worker")):
            with self.subTest(who=who):
                line = self.task_line(**finished_stopped(who))
                self.assertIn(f"중지됨({label})", line)
                self.assertNotIn("보고 완료", line)

    def test_unknown_requester_is_shown_sanitized_and_capped(self):
        line = self.task_line(**finished_stopped("watchdog"))
        self.assertIn("중지됨(watchdog)", line)
        long = "x" * 200
        line = self.task_line(**finished_stopped(long))
        self.assertIn("중지됨(", line)
        self.assertNotIn("x" * 60, line)
        self.assertLess(len(line), 140)

    def test_unknown_requester_control_chars_removed(self):
        for evil in ("a\x1b[31mred\x07b", "tab\tnew\nline\r", "\x00\x01\x02", "\x1b]0;title\x07x", "ab‮cd\x7f"):
            with self.subTest(evil=evil):
                model = self.render(**finished_stopped(evil))
                self.assertClean(model)
                self.assertNotIn("보고 완료", model.task_text())
                self.assertTrue(model.task_text().strip())

    def test_missing_empty_or_nonstring_requester_is_plain_stopped(self):
        cases = [("__missing__",), (None,), ("",), ("   ",), (123,), (["user"],), ({"x": "user"},), (True,), (1.5,)]
        for (value,) in cases:
            with self.subTest(value=value):
                line = self.task_line(**finished_stopped(value))
                self.assertIn("[중지됨]", line)
                self.assertNotIn("중지됨(", line)
                self.assertNotIn("사용자", line)
                self.assertNotIn("보고 완료", line)

    def test_other_finished_outcomes_keep_labels(self):
        report = self.task_line(status="finished", last_result={"outcome": "reported"})
        self.assertIn("보고 완료", report)
        self.assertNotIn("중지됨", report)
        for last in ({"outcome": "completed"}, None, {}, {"stopped_by": "user"}):
            with self.subTest(last=last):
                line = self.task_line(status="finished", last_result=last)
                self.assertIn("보고 완료", line)
                self.assertNotIn("중지됨", line)
        failed = self.task_line(status="finished", held_reason="start_failed:WorkflowHeld",
                                last_result={"outcome": "start_failed", "error": "WorkflowHeld"})
        self.assertIn("시작 실패", failed)
        self.assertNotIn("중지됨", failed)
        not_started = self.task_line(status="finished", last_result={"outcome": "not_started", "reason": "r"})
        self.assertIn("시작 실패", not_started)

    def test_non_finished_status_with_stopped_outcome_is_unaffected(self):
        expected = {"running": "실행 중", "cancelling": "취소 중", "waiting_report": "보고 대기", "held": "보류",
                    "blocked": "막힘", "dispatched": "전달됨", "starting": "시작 중"}
        for status, text in expected.items():
            with self.subTest(status=status):
                line = self.task_line(status=status, last_result={"outcome": "stopped", "stopped_by": "user"})
                self.assertIn(text, line)
                self.assertNotIn("중지됨", line)
        closed = self.task_line(status="closed", closed_reason="cancelled",
                                last_result={"outcome": "stopped", "stopped_by": "user"})
        self.assertIn("종료(취소됨)", closed)
        self.assertNotIn("중지됨", closed)

    def test_stopped_label_composes_with_work_kind_and_notes(self):
        line = self.task_line(kind="work", **finished_stopped("manager", host_commands=[cmd("unknown")]))
        self.assertIn("작업: 작업", line)
        self.assertIn("중지됨(Manager)", line)
        self.assertIn(UNKNOWN_NOTE, line)


class HostCommandNoteTests(Base):
    def test_note_selection_table(self):
        cases = [
            ({"host_commands": [cmd("running")]}, [RUNNING_NOTE]),
            ({"host_commands": [cmd("stopping")]}, [RUNNING_NOTE]),
            ({"host_commands": [cmd("unknown")]}, [UNKNOWN_NOTE]),
            ({"host_commands": [cmd("unknown"), cmd("running")]}, [RUNNING_NOTE]),
            ({"host_commands": [cmd("unknown"), cmd("stopping")]}, [RUNNING_NOTE]),
            ({"host_commands": [cmd("unknown"), cmd("unknown")]}, [UNKNOWN_NOTE]),
            ({"host_commands": [cmd("running"), cmd("running")]}, [RUNNING_NOTE]),
            ({"host_commands": []}, []),
            ({}, []),
            ({"host_commands": [cmd("ended")]}, []),
            ({"host_commands": [cmd("exited"), cmd("stopped")]}, []),
            ({"host_commands": [], "host_commands_unavailable": True}, [UNAVAILABLE_NOTE]),
            ({"host_commands": [cmd("running")], "host_commands_unavailable": True}, [UNAVAILABLE_NOTE]),
            ({"host_commands": [cmd("unknown")], "host_commands_unavailable": True}, [UNAVAILABLE_NOTE]),
            ({"host_commands_unavailable": True}, [UNAVAILABLE_NOTE]),
            ({"host_commands": [cmd("running")], "host_commands_unavailable": False}, [RUNNING_NOTE]),
            ({"host_commands": [], "host_commands_unavailable": False}, []),
            ({"host_commands": [cmd("running")], "host_commands_unavailable": None}, [RUNNING_NOTE]),
        ]
        statuses = [
            {"status": "running"},
            {"status": "cancelling", "cancel_requested": True},
            {"status": "closed", "closed_reason": "cancelled", "active": False},
            {"status": "closed", "closed_reason": "done", "active": False},
            finished_stopped("user"),
            {"status": "finished", "last_result": {"outcome": "reported"}},
            {"status": "held", "held_reason": "needs_report_or_cancel"},
        ]
        for status in statuses:
            for extra, expected in cases:
                with self.subTest(status=status["status"], extra=extra):
                    line = self.task_line(**{**status, **copy.deepcopy(extra)})
                    self.assertEqual(self.notes_in(line), expected, line)
                    self.assertEqual(sum(line.count(n) for n in ALL_NOTES), len(expected), line)
                    self.assertClean([line])

    def test_note_is_inside_task_brackets_after_status(self):
        line = self.task_line(status="closed", closed_reason="cancelled", active=False,
                              host_commands=[cmd("running")])
        self.assertRegex(line, r"\[종료\(취소됨\) · 명령 실행 중\]")

    def test_note_follows_other_notes(self):
        line = self.task_line(status="dispatched", held_reason="host_terminal_busy", cancel_requested=True,
                              host_commands=[cmd("running")])
        self.assertTrue(line.index("host terminal 사용 중") < line.index("취소 요청됨") < line.index(RUNNING_NOTE), line)

    def test_no_task_no_note(self):
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0)
        model.apply_snapshot(snapshot())
        self.assertEqual(model.task_text(), "")
        for line in model.status_lines():
            self.assertEqual(self.notes_in(line), [])

    def test_unavailable_truthy_non_bool_is_not_true(self):
        for value in ("yes", 1, "true", [1], {"a": 1}, 1.0, "True"):
            with self.subTest(value=value):
                line = self.task_line(host_commands=[], host_commands_unavailable=value)
                self.assertEqual(self.notes_in(line), [], line)
                line = self.task_line(host_commands=[cmd("running")], host_commands_unavailable=value)
                self.assertEqual(self.notes_in(line), [RUNNING_NOTE], line)

    def test_state_text_is_exact_not_substring_or_case_folded(self):
        for state in ("Running", "RUNNING", " running", "running ", "not_running", "unknown2", "Unknown", ""):
            with self.subTest(state=state):
                line = self.task_line(host_commands=[cmd(state)])
                self.assertEqual(self.notes_in(line), [], line)

    def test_never_displays_command_id_or_log_path(self):
        for state in ("running", "stopping", "unknown"):
            with self.subTest(state=state):
                task = {"host_commands": [cmd(state, stop={"requested_by": "user", "stage": "term"},
                                              command="python train.py --secret")]}
                model = self.render(**task, **finished_stopped("user"))
                self.assertClean(model)
                self.assertNotIn("train.py", "\n".join(model.status_lines()))

    def test_updates_follow_the_latest_snapshot(self):
        model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0)
        model.apply_snapshot(task_snapshot(host_commands=[cmd("running")]))
        self.assertIn(RUNNING_NOTE, model.task_text())
        model.apply_snapshot(task_snapshot(host_commands=[]))
        self.assertEqual(self.notes_in(model.task_text()), [])
        model.apply_snapshot(task_snapshot(host_commands=[cmd("unknown")]))
        self.assertEqual(self.notes_in(model.task_text()), [UNKNOWN_NOTE])


class MalformedInputTests(Base):
    def test_malformed_never_raises_or_prints_controls(self):
        weird = [None, 0, 1, "yes", "", "running", b"running", 1.5, True, [], {}, ["running"], {"state": "running"},
                 [None], [1], ["running"], [[cmd("running")]], [{"state": None}], [{"state": 5}],
                 [{"state": ["running"]}], [{"state": {"a": "running"}}], [{"state": b"running"}], [{}], [{"x": 1}],
                 [None, cmd("unknown")], ["x", cmd("running")], [{"state": "run\x1bning"}],
                 [{"state": "\x00"}], {"running": cmd("running")}, "[{'state': 'running'}]", (cmd("running"),)]
        for value in weird:
            for unavailable in ("__missing__", True, "yes", None, [], {}, 0):
                task = {"host_commands": value}
                if unavailable != "__missing__":
                    task["host_commands_unavailable"] = unavailable
                with self.subTest(host_commands=value, unavailable=unavailable):
                    model = self.render(**task)
                    self.assertClean(model)
                    self.assertTrue(model.task_text().startswith("작업:"))

    def test_malformed_values_in_list_are_ignored_not_misread(self):
        # tuple is not a list (JSON never sends one) and a dict is not a list: no note either way.
        for value in ({"state": "running"}, "running", 5, None):
            with self.subTest(value=value):
                self.assertEqual(self.notes_in(self.task_line(host_commands=value)), [])
        self.assertEqual(self.notes_in(self.task_line(host_commands=[None, "x", 3, {"state": ["running"]}])), [])
        # a good item next to bad ones still counts
        self.assertEqual(self.notes_in(self.task_line(host_commands=[None, "x", cmd("running")])), [RUNNING_NOTE])

    def test_last_result_wrong_types(self):
        for last in ("stopped", ["stopped"], 7, None, True, {"outcome": ["stopped"]}, {"outcome": {"a": 1}},
                     {"outcome": None}, {"outcome": 5}, {"outcome": "stopped\x1b"}, {"outcome": " stopped "}):
            with self.subTest(last=last):
                model = self.render(status="finished", last_result=last)
                self.assertClean(model)
                self.assertNotIn("중지됨(", model.task_text())

    def test_stopped_outcome_must_be_exactly_stopped_string(self):
        for outcome in ("Stopped", "STOPPED", "stopped_by_user", "stop"):
            with self.subTest(outcome=outcome):
                line = self.task_line(status="finished", last_result={"outcome": outcome, "stopped_by": "user"})
                self.assertNotIn("중지됨", line)

    def test_whole_task_garbage_fields(self):
        for key in ("status", "kind", "summary", "closed_reason", "held_reason", "cancel_requested", "last_result"):
            for value in (None, 1, [], {}, "\x1b[2J", ["x"], {"a": 1}):
                with self.subTest(key=key, value=value):
                    model = self.render(host_commands=[cmd("unknown")], **{key: value})
                    self.assertClean(model)

    def test_snapshot_task_not_a_dict(self):
        for value in (None, [], "x", 5):
            model = ProductModel(FakeSender(), 30, 120, clock=lambda: 1000.0)
            snap = snapshot()
            snap["task"] = value
            model.apply_snapshot(snap)
            self.assertClean(model)


class WidthTests(Base):
    def test_fits_80_columns_typical_cases(self):
        cases = [
            dict(status="closed", closed_reason="cancelled", active=False, host_commands=[cmd("running")]),
            dict(status="closed", closed_reason="cancelled", active=False, host_commands=[cmd("unknown")]),
            dict(status="closed", closed_reason="cancelled", active=False, host_commands=[],
                 host_commands_unavailable=True),
            dict(status="cancelling", cancel_requested=True, host_commands=[cmd("stopping")]),
            finished_stopped("user", host_commands=[cmd("unknown")]),
            finished_stopped("manager", host_commands=[cmd("running")]),
            finished_stopped("worker", host_commands=[], host_commands_unavailable=True),
            dict(status="running", host_commands=[cmd("running")]),
        ]
        for task in cases:
            with self.subTest(task=task["status"], extra=sorted(k for k in task if k.startswith("host"))):
                model = self.render(80, **task)
                text = model.task_text()
                self.assertLessEqual(wcswidth(text), 80, (wcswidth(text), text))
                self.assertTrue(any(n in text for n in ALL_NOTES) or "중지됨" in text, text)
                # status_lines() carries other segments (focus/backend) that are not this Task's concern:
                # the Task segment must be one whole piece of one of its lines, never split by this change.
                self.assertTrue(any(text in line for line in model.status_lines()), model.status_lines())

    def test_note_survives_in_task_text_with_long_summary(self):
        # The summary is clipped before the notes so the note is not what is lost.
        model = self.render(80, summary="가" * 200, status="closed", closed_reason="cancelled", active=False,
                            host_commands=[cmd("running")])
        self.assertIn(RUNNING_NOTE, model.task_text())
        self.assertIn("종료(취소됨)", model.task_text())

    def test_stopped_requester_cap_keeps_line_bounded(self):
        model = self.render(80, summary="s", **finished_stopped("가" * 300, host_commands=[cmd("unknown")]))
        text = model.task_text()
        self.assertLess(wcswidth(text), 100, text)
        self.assertIn(UNKNOWN_NOTE, text)


if __name__ == "__main__":
    unittest.main()
