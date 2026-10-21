"""tmux auto-setup (mouse / allow-passthrough) behind its public function, with injected tmux, prompt and home.

Nothing here reads the real ~/.tmux.conf or talks to the user's tmux server: every test passes a temp home and a
fake (or private-socket) tmux runner, and the real TMUX variable never reaches the function.
"""
import itertools
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from workbench.ui.tmux_setup import ensure_tmux_options, tmux_runner

TMUX_ENV = {"TMUX": "/tmp/fake-sock,1,0"}


class FakeTmux:
    """Records argv; ``values`` is the running server's global options. ``unsupported`` options fail like tmux < 3.3."""

    def __init__(self, values=None, unsupported=(), fail_set=(), error=None):
        self.values = {"mouse": "off", "allow-passthrough": "off"} if values is None else dict(values)
        self.unsupported, self.fail_set, self.error = set(unsupported), set(fail_set), error
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        if args[:2] == ["show", "-gv"]:
            if args[2] in self.unsupported:
                return 1, ""
            return 0, self.values[args[2]] + "\n"
        if args[:2] == ["set", "-g"]:
            if args[2] in self.fail_set:
                return 1, ""
            self.values[args[2]] = args[3]
            return 0, ""
        raise AssertionError(f"unexpected tmux call {args}")

    def sets(self):
        return [c for c in self.calls if c[0] == "set"]


class Ask:
    def __init__(self, answer="", error=None):
        self.answer, self.error, self.prompts = answer, error, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.answer


class TmuxSetupCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="tmux-setup-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)

    def run_setup(self, tmux, *, environ=TMUX_ENV, interactive=False, ask=None):
        return ensure_tmux_options(environ=environ, run=tmux, interactive=interactive,
                                   ask=ask or Ask(), home=self.home)


class AlreadySetTests(TmuxSetupCase):
    def test_b1_all_set_does_nothing_at_all(self):
        for passthrough in ("on", "all"):
            with self.subTest(passthrough=passthrough):
                tmux, ask = FakeTmux({"mouse": "on", "allow-passthrough": passthrough}), Ask()
                result = self.run_setup(tmux, interactive=True, ask=ask)
                self.assertEqual([], tmux.sets())
                self.assertEqual([], ask.prompts)
                self.assertEqual("", result.notice)
                self.assertEqual((), result.applied)
                self.assertIsNone(result.written_path)
                self.assertEqual([], list(self.home.iterdir()))

    def test_b1_outside_tmux_never_calls_tmux(self):
        tmux = FakeTmux()
        result = self.run_setup(tmux, environ={})
        self.assertEqual([], tmux.calls)
        self.assertEqual("", result.notice)


class RuntimeOnlyTests(TmuxSetupCase):
    def test_b2_missing_without_tty_sets_only_the_missing_options_on_the_running_server(self):
        tmux, ask = FakeTmux({"mouse": "on", "allow-passthrough": "off"}), Ask()
        result = self.run_setup(tmux, interactive=False, ask=ask)
        self.assertEqual([["set", "-g", "allow-passthrough", "on"]], tmux.sets())
        self.assertEqual([], ask.prompts)
        self.assertEqual(("allow-passthrough",), result.applied)
        self.assertIsNone(result.written_path)
        self.assertEqual("tmux 설정 적용: allow-passthrough on", result.notice)
        self.assertEqual([], list(self.home.iterdir()))

    def test_b2_both_missing_are_both_applied(self):
        tmux = FakeTmux()
        result = self.run_setup(tmux)
        self.assertEqual([["set", "-g", "mouse", "on"], ["set", "-g", "allow-passthrough", "on"]], tmux.sets())
        self.assertEqual("tmux 설정 적용: mouse on, allow-passthrough on", result.notice)


class PromptTests(TmuxSetupCase):
    def test_b3_tty_prompts_once_in_korean_and_anything_but_yes_applies_to_the_running_server_only(self):
        for answer in ("", "n", "N", "아니오", "ye", "yess", "  "):
            with self.subTest(answer=answer):
                tmux, ask = FakeTmux(), Ask(answer)
                result = self.run_setup(tmux, interactive=True, ask=ask)
                self.assertEqual(1, len(ask.prompts))
                prompt = ask.prompts[0]
                self.assertIn("tmux 설정이 없습니다: mouse, allow-passthrough", prompt)
                self.assertIn("~/.tmux.conf 에 추가할까요?", prompt)  # the config path that would be written, home-relative
                self.assertIn("[y/N]", prompt)
                self.assertEqual(["mouse", "allow-passthrough"], [c[2] for c in tmux.sets()])
                self.assertIsNone(result.written_path)
                self.assertEqual("tmux 설정 적용: mouse on, allow-passthrough on", result.notice)
                self.assertEqual([], list(self.home.iterdir()))

    def test_b3_eof_and_interrupt_on_the_prompt_count_as_no(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                tmux = FakeTmux()
                result = self.run_setup(tmux, interactive=True, ask=Ask(error=error))
                self.assertEqual(2, len(tmux.sets()))
                self.assertIsNone(result.written_path)
                self.assertEqual([], list(self.home.iterdir()))

    def test_b3_prompt_names_only_the_missing_options(self):
        ask = Ask("n")
        self.run_setup(FakeTmux({"mouse": "on", "allow-passthrough": "off"}), interactive=True, ask=ask)
        self.assertIn("tmux 설정이 없습니다: allow-passthrough\n", ask.prompts[0])


BEGIN, END = "# >>> workbench tmux >>>", "# <<< workbench tmux <<<"


class ConfigFileTests(TmuxSetupCase):
    def yes(self, tmux=None, environ=TMUX_ENV, **extra):
        tmux = tmux or FakeTmux(**extra)
        return tmux, ensure_tmux_options(environ=environ, run=tmux, interactive=True, ask=Ask("y"), home=self.home)

    def test_b4_yes_creates_tilde_tmux_conf_with_a_marked_block_and_applies_to_the_server(self):
        for answer in ("y", "Y", "yes", " YES "):
            with self.subTest(answer=answer):
                (self.home / ".tmux.conf").unlink(missing_ok=True)
                tmux = FakeTmux()
                result = ensure_tmux_options(environ=TMUX_ENV, run=tmux, interactive=True, ask=Ask(answer),
                                             home=self.home)
                path = self.home / ".tmux.conf"
                self.assertEqual(f"{BEGIN}\nset -g mouse on\nset -g allow-passthrough on\n{END}\n",
                                 path.read_text())
                self.assertEqual(path, result.written_path)
                self.assertEqual(["mouse", "allow-passthrough"], [c[2] for c in tmux.sets()])
                self.assertEqual("tmux 설정 적용: mouse on, allow-passthrough on (~/.tmux.conf에 추가)", result.notice)

    def test_b4_block_holds_only_the_missing_lines(self):
        tmux, result = self.yes(values={"mouse": "on", "allow-passthrough": "off"})
        self.assertEqual(f"{BEGIN}\nset -g allow-passthrough on\n{END}\n", (self.home / ".tmux.conf").read_text())

    def test_b4_appends_without_touching_existing_content(self):
        for original in ("set -g history-limit 5000\n", "set -g history-limit 5000", ""):
            with self.subTest(original=original):
                path = self.home / ".tmux.conf"
                path.write_text(original)
                self.yes()
                text = path.read_text()
                self.assertTrue(text.startswith(original))
                self.assertEqual(1, text.count(BEGIN))
                self.assertIn(f"{BEGIN}\nset -g mouse on\n", text)
                self.assertTrue(text[len(original):].lstrip("\n").startswith(BEGIN))

    def test_b4_prefers_an_existing_xdg_config_when_there_is_no_tilde_tmux_conf(self):
        xdg = self.home / "xdgcfg" / "tmux" / "tmux.conf"
        xdg.parent.mkdir(parents=True)
        xdg.write_text("# mine\n")
        _, result = self.yes(environ={**TMUX_ENV, "XDG_CONFIG_HOME": str(self.home / "xdgcfg")})
        self.assertEqual(xdg, result.written_path)
        self.assertIn(BEGIN, xdg.read_text())
        self.assertFalse((self.home / ".tmux.conf").exists())

    def test_b4_default_xdg_location_is_dot_config_and_tilde_wins_when_both_exist(self):
        default_xdg = self.home / ".config" / "tmux" / "tmux.conf"
        default_xdg.parent.mkdir(parents=True)
        default_xdg.write_text("")
        _, result = self.yes()
        self.assertEqual(default_xdg, result.written_path)
        self.assertIn("(tmux.conf에 추가)", result.notice)  # the full path would be 80 columns: name only
        default_xdg.write_text("")
        (self.home / ".tmux.conf").write_text("")
        _, result = self.yes()
        self.assertEqual(self.home / ".tmux.conf", result.written_path)

    def test_b4_an_existing_workbench_block_is_not_duplicated(self):
        _, first = self.yes()
        before = (self.home / ".tmux.conf").read_text()
        ask, tmux = Ask("y"), FakeTmux()  # a server that started before the block was written still lacks the options
        result = ensure_tmux_options(environ=TMUX_ENV, run=tmux, interactive=True, ask=ask, home=self.home)
        self.assertEqual(before, (self.home / ".tmux.conf").read_text())
        self.assertEqual(1, (self.home / ".tmux.conf").read_text().count(BEGIN))
        self.assertIsNone(result.written_path)
        self.assertEqual(2, len(tmux.sets()))  # still applied to the running server
        self.assertEqual([], ask.prompts)  # nothing to write, so nothing to ask
        self.assertEqual("tmux 설정 적용: mouse on, allow-passthrough on", result.notice)


class UnsupportedOptionTests(TmuxSetupCase):
    def test_b5_allow_passthrough_unknown_to_old_tmux_is_skipped_everywhere_but_mouse_is_handled(self):
        tmux, ask = FakeTmux(unsupported={"allow-passthrough"}), Ask("y")
        result = ensure_tmux_options(environ=TMUX_ENV, run=tmux, interactive=True, ask=ask, home=self.home)
        self.assertIn("tmux 설정이 없습니다: mouse\n", ask.prompts[0])
        self.assertEqual([["set", "-g", "mouse", "on"]], tmux.sets())
        self.assertEqual(f"{BEGIN}\nset -g mouse on\n{END}\n", (self.home / ".tmux.conf").read_text())
        self.assertEqual(("mouse",), result.applied)
        self.assertEqual("tmux 설정 적용: mouse on (~/.tmux.conf에 추가)", result.notice)

    def test_b5_unsupported_only_with_mouse_already_on_is_a_complete_noop(self):
        tmux, ask = FakeTmux({"mouse": "on", "allow-passthrough": "off"}, unsupported={"allow-passthrough"}), Ask()
        result = self.run_setup(tmux, interactive=True, ask=ask)
        self.assertEqual([], tmux.sets())
        self.assertEqual([], ask.prompts)
        self.assertEqual("", result.notice)


class NeverBlockStartupTests(TmuxSetupCase):
    def test_b6_unreadable_tmux_is_a_short_notice_not_an_exception_or_prompt(self):
        errors = (subprocess.TimeoutExpired(["tmux"], 2), OSError("boom"), subprocess.SubprocessError("x"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                ask = Ask("y")
                result = self.run_setup(FakeTmux(error=error), interactive=True, ask=ask)
                self.assertEqual("tmux 설정 확인 실패 — 건너뜀", result.notice)
                self.assertEqual([], ask.prompts)
                self.assertEqual((), result.applied)
                self.assertEqual([], list(self.home.iterdir()))

    def test_b6_missing_tmux_binary_or_dead_server_status(self):
        result = self.run_setup(FakeTmux(error=FileNotFoundError("tmux")))
        self.assertEqual("", result.notice)  # no tmux to talk to: nothing to set up, nothing to say
        dead = lambda args: (1, "")  # noqa: E731  stale $TMUX: `no server running`
        result = ensure_tmux_options(environ=TMUX_ENV, run=dead, interactive=False, ask=Ask(), home=self.home)
        self.assertEqual("tmux 설정 확인 실패 — 건너뜀", result.notice)

    def test_b6_failed_set_is_reported_and_only_successes_count_as_applied(self):
        tmux = FakeTmux(fail_set={"allow-passthrough"})
        result = self.run_setup(tmux)
        self.assertEqual(("mouse",), result.applied)
        self.assertEqual("tmux 설정 적용: mouse on · 적용 실패: allow-passthrough", result.notice)
        result = self.run_setup(FakeTmux(fail_set={"mouse", "allow-passthrough"}))
        self.assertEqual("tmux 설정 적용 실패: mouse, allow-passthrough", result.notice)

    def test_b6_timeout_while_setting_is_a_failed_set_not_an_exception(self):
        class SlowSet(FakeTmux):
            def __call__(self, args):
                if args[0] == "set":
                    raise subprocess.TimeoutExpired(["tmux"], 2)
                return super().__call__(args)
        result = self.run_setup(SlowSet())
        self.assertEqual("tmux 설정 적용 실패: mouse, allow-passthrough", result.notice)

    def test_b6_unwritable_config_still_applies_to_the_running_server(self):
        (self.home / ".tmux.conf").mkdir()  # open("ab") on a directory fails
        tmux = FakeTmux()
        result = ensure_tmux_options(environ=TMUX_ENV, run=tmux, interactive=True, ask=Ask("y"), home=self.home)
        self.assertEqual(2, len(tmux.sets()))
        self.assertIsNone(result.written_path)
        self.assertEqual("tmux 설정 적용: mouse on, allow-passthrough on · ~/.tmux.conf 쓰기 실패", result.notice)

    def test_b6_unreadable_existing_block_check_does_not_raise(self):
        (self.home / ".tmux.conf").mkdir()
        result = ensure_tmux_options(environ=TMUX_ENV, run=FakeTmux(), interactive=True, ask=Ask("y"), home=self.home)
        self.assertEqual(("mouse", "allow-passthrough"), result.applied)

    def test_b6_notice_fits_79_display_columns_even_for_a_long_config_path(self):
        from wcwidth import wcswidth
        deep = self.home / ("가" * 30) / "tmux" / "tmux.conf"
        deep.parent.mkdir(parents=True)
        deep.write_text("")
        result = ensure_tmux_options(environ={**TMUX_ENV, "XDG_CONFIG_HOME": str(self.home / ("가" * 30))},
                                     run=FakeTmux(), interactive=True, ask=Ask("y"), home=self.home)
        self.assertEqual(deep, result.written_path)
        self.assertLessEqual(wcswidth(result.notice), 79)
        self.assertTrue(result.notice.startswith("tmux 설정 적용: mouse on, allow-passthrough on"))
        for notice in ("tmux 설정 적용: mouse on, allow-passthrough on (~/.tmux.conf에 추가)",):
            self.assertLessEqual(wcswidth(notice), 79)

    def test_b6_every_failure_combination_fits_79_columns(self):
        from wcwidth import wcswidth
        for fail in ({"allow-passthrough"}, {"mouse"}, {"mouse", "allow-passthrough"}, set()):
            for relative, writable in itertools.product((".tmux.conf", ".config/tmux/tmux.conf"), (False, True)):
                with self.subTest(fail=sorted(fail), config=relative, writable=writable):
                    home = Path(tempfile.mkdtemp(prefix="tmux-setup-width-"))
                    self.addCleanup(shutil.rmtree, home, True)
                    path = home / relative
                    if relative != ".tmux.conf":
                        path.parent.mkdir(parents=True)
                    if writable:
                        path.write_text("")
                    else:
                        path.mkdir()  # not a file: the write fails
                    result = ensure_tmux_options(environ=TMUX_ENV, run=FakeTmux(fail_set=fail), interactive=True,
                                                 ask=Ask("y"), home=home)
                    self.assertLessEqual(wcswidth(result.notice), 79, result.notice)

    def test_b6_very_long_wide_config_path_with_failures_still_fits_79_columns(self):
        from wcwidth import wcswidth
        xdg = self.home / ("가나다" * 20) / ("라마바" * 20)
        config = xdg / "tmux" / "tmux.conf"
        config.parent.mkdir(parents=True)
        environ = {**TMUX_ENV, "XDG_CONFIG_HOME": str(xdg)}
        for fail in ({"allow-passthrough"}, {"mouse", "allow-passthrough"}):
            for writable in (True, False):
                with self.subTest(fail=sorted(fail), writable=writable):
                    if config.is_dir():
                        config.rmdir()
                    else:
                        config.unlink(missing_ok=True)
                    if writable:
                        config.write_text("")
                    else:
                        config.mkdir()
                    result = ensure_tmux_options(environ=environ, run=FakeTmux(fail_set=fail), interactive=True,
                                                 ask=Ask("y"), home=self.home)
                    self.assertLessEqual(wcswidth(result.notice), 79, result.notice)
                    self.assertTrue(result.notice.startswith("tmux 설정 적용"), result.notice)


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class RealTmuxTests(TmuxSetupCase):
    """A private server on a temp socket (``-f /dev/null``): the user's own tmux server is never involved."""

    def setUp(self):
        super().setUp()
        self.sock = str(self.home / "sock")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}  # no inherited TMUX
        self.addCleanup(self.kill_server)
        subprocess.run(["tmux", "-S", self.sock, "-f", "/dev/null", "new-session", "-d", "-s", "wbtest"],
                       env=self.env, check=True, timeout=10, capture_output=True)
        pid = self.tmux("display-message", "-p", "#{pid}").strip()
        self.environ = {**self.env, "TMUX": f"{self.sock},{pid},0"}  # what tmux puts in a pane's environment

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", self.sock, *args], env=self.env, check=True, timeout=10,
                              capture_output=True, text=True).stdout

    def kill_server(self):
        subprocess.run(["tmux", "-S", self.sock, "kill-server"], env=self.env, timeout=10, capture_output=True)

    def test_b7_real_server_gets_both_options_and_the_config_file(self):
        self.assertEqual("off\n", self.tmux("show", "-gv", "mouse"))
        result = ensure_tmux_options(environ=self.environ, run=tmux_runner(self.environ), interactive=True,
                                     ask=Ask("y"), home=self.home)
        self.assertEqual("on\n", self.tmux("show", "-gv", "mouse"))
        self.assertEqual("on\n", self.tmux("show", "-gv", "allow-passthrough"))
        self.assertEqual("tmux 설정 적용: mouse on, allow-passthrough on (~/.tmux.conf에 추가)", result.notice)
        self.assertIn("set -g allow-passthrough on", (self.home / ".tmux.conf").read_text())
        again = ensure_tmux_options(environ=self.environ, run=tmux_runner(self.environ), interactive=True,
                                    ask=Ask("y"), home=self.home)
        self.assertEqual("", again.notice)  # already set: nothing to do the second time

    def test_b7_all_counts_as_already_set(self):
        self.tmux("set", "-g", "mouse", "on")
        self.tmux("set", "-g", "allow-passthrough", "all")
        result = ensure_tmux_options(environ=self.environ, run=tmux_runner(self.environ), interactive=False,
                                     ask=Ask(), home=self.home)
        self.assertEqual("", result.notice)
        self.assertEqual("all\n", self.tmux("show", "-gv", "allow-passthrough"))

    def test_b7_stale_socket_is_a_notice_not_a_crash(self):
        self.kill_server()
        result = ensure_tmux_options(environ=self.environ, run=tmux_runner(self.environ), interactive=False,
                                     ask=Ask(), home=self.home)
        self.assertEqual("tmux 설정 확인 실패 — 건너뜀", result.notice)


if __name__ == "__main__":
    unittest.main()
