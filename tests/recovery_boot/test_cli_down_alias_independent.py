"""DOWN-ALIAS: down preserves shutdown parsing, confirmation and result semantics.

Fixture boundary: the real parser/main/handler run against the existing fake
ui_v1 client. No backend, shell, credential store or user demo is started.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_cli_independent_p27cw19 import FakeClient
from workbench.backend import cli
from workbench.contracts.ui_v1 import ClientType


class DownAliasTests(unittest.TestCase):
    def test_options_defaults_and_handler_match_shutdown(self):
        for options in ([], ["--yes"], ["--json"], ["--data-dir", "/fixture/data", "--json", "--yes"]):
            with self.subTest(options=options):
                shutdown = vars(cli.parser().parse_args(["shutdown", *options]))
                down = vars(cli.parser().parse_args(["down", *options]))
                self.assertIs(down["handler"], cli.cmd_shutdown)
                self.assertEqual(shutdown.pop("command"), "shutdown")
                self.assertEqual(down.pop("command"), "down")
                self.assertEqual(down, shutdown)

    def test_help_and_bad_options_exit_before_shutdown_handler(self):
        for option, expected in (("--help", 0), ("--not-an-option", 2)):
            results = []
            for command in ("shutdown", "down"):
                out, err = io.StringIO(), io.StringIO()
                with patch.object(cli, "UiClient") as client, redirect_stdout(out), redirect_stderr(err):
                    with self.assertRaises(SystemExit) as stopped:
                        cli.main([command, option])
                    self.assertEqual(stopped.exception.code, expected)
                    client.assert_not_called()
                results.append((out.getvalue(), err.getvalue()))
            self.assertEqual(results[0], results[1])

    def _run(self, command, *, result, yes=True, json_form=False, tty=False, answer="n"):
        client = FakeClient({ClientType.SHUTDOWN_REQUEST: {"ok": True, "token": "fixture-token",
                                                          "active": [{"kind": "task_run", "id": "fixture"}]},
                             ClientType.SHUTDOWN_CONFIRM: {"ok": True}},
                            closing=None if result is None else {"result": result})
        arguments = [command, "--data-dir", "/fixture/data"]
        if yes:
            arguments.append("--yes")
        if json_form:
            arguments.append("--json")
        stdin = SimpleNamespace(isatty=lambda: tty)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "_layout", return_value=SimpleNamespace(root=Path("/fixture/data"),
                                                                       ui_socket=Path("/fixture/ui.sock"))), \
                patch.object(cli, "UiClient", return_value=client), \
                patch.object(cli, "_backend_state", return_value="unknown"), \
                patch.object(cli.sys, "stdin", stdin), patch("builtins.input", return_value=answer) as prompt, \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(arguments)
        return code, out.getvalue(), err.getvalue(), client.sent, prompt.call_args_list

    def test_success_failure_and_missing_result_have_identical_exit_output_and_requests(self):
        for result, expected in (({"verified": True, "problems": []}, 0),
                                 ({"verified": False, "problems": ["owned_process_alive"]}, 1), (None, 1)):
            for json_form in (False, True):
                with self.subTest(result=result, json=json_form):
                    shutdown = self._run("shutdown", result=result, json_form=json_form)
                    down = self._run("down", result=result, json_form=json_form)
                    self.assertEqual(down, shutdown)
                    self.assertEqual(down[0], expected)
                    self.assertEqual(down[3], [(ClientType.SHUTDOWN_REQUEST, {}),
                                               (ClientType.SHUTDOWN_CONFIRM, {"token": "fixture-token"})])
                    self.assertFalse(down[4], "--yes must not ask for interactive confirmation")

    def test_confirmation_is_not_bypassed_by_alias(self):
        for tty, answer, expected in ((False, "y", 1), (True, "n", 1), (True, "y", 0)):
            with self.subTest(tty=tty, answer=answer):
                options = dict(result={"verified": True}, yes=False, tty=tty, answer=answer)
                shutdown, down = self._run("shutdown", **options), self._run("down", **options)
                self.assertEqual(down, shutdown)
                self.assertEqual(down[0], expected)
                confirmed = any(kind is ClientType.SHUTDOWN_CONFIRM for kind, _ in down[3])
                self.assertEqual(confirmed, tty and answer == "y")
                self.assertIn("task_run", down[1], "active work remains visible before confirmation")
                self.assertEqual(bool(down[4]), tty)


if __name__ == "__main__":
    unittest.main()
