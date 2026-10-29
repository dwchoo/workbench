"""HSB (docs/features/host-shell-bashrc/BRIEF.md): the host shell applies the user's ``~/.bashrc``.

Real ``ShellPane`` -> ``PersistentShell`` -> ``/usr/bin/bash`` with a temp ``HOME`` holding a crafted
``.bashrc`` (the real one is never read). Expected values are written by the test itself: the rc's own
alias/function/variable outputs land in files the test reads, completion detection is observed as the
pane's ``parent_mode`` returning to ``manual_prompt`` (a fresh READY) and as a managed request's
``RETURN:<status>`` plus the controller exit code.
"""
from pathlib import Path
import os
import tempfile
import time
import unittest
from uuid import uuid4

from workbench.backend.panes import ShellPane
from workbench.terminal.shell_g2.prototype import ShellChoice

BASH = ShellChoice("bash", "/usr/bin/bash")


class BashrcCase(unittest.TestCase):
    def start(self, rc: str | None, *, setup=None):
        directory = tempfile.TemporaryDirectory(prefix="hsb-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        if rc is not None:
            (self.home / ".bashrc").write_text(rc)
        if setup is not None:
            setup(self.home)
        self.shown = bytearray()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8", "TERM": "xterm"}
        started = time.monotonic()
        self.pane = ShellPane(BASH, env)
        self.startup_seconds = time.monotonic() - started
        self.addCleanup(self.pane.close)
        self.until(lambda: self.pane.state["parent_mode"] == "manual_prompt")
        return self.pane

    def pump(self):
        for chunk in self.pane.pump():
            self.shown.extend(chunk.data)

    def until(self, condition, seconds=10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump()
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError({"timeout": self.pane.shell.snapshot(), "shown": bytes(self.shown[-400:])})

    def type_line(self, line: str):
        """A manual line; its completion is the next fresh READY (parent_mode back to manual_prompt)."""
        before = len(self.pane.shell._transport.events)
        self.assertIsNone(self.pane.admit(line.encode() + b"\r"))
        self.until(lambda: "READY" in self.pane.shell._transport.events[before:]
                   and self.pane.state["parent_mode"] == "manual_prompt")

    def output(self, line: str) -> str:
        out = self.home / f"out-{uuid4().hex}"
        self.type_line(f"{{ {line}; }} > {out} 2>&1")
        return out.read_text()

    def run_managed(self, command: str) -> dict:
        """wb-handoff, claim, one approved request; returns the lifecycle once the controller returned."""
        self.assertIsNone(self.pane.admit(b"wb-handoff\r"))
        self.until(lambda: self.pane.state["parent_mode"] == "control_wait")
        shell = self.pane.shell
        self.pane.state = shell.claim_manager()
        state = shell.snapshot()
        shell.submit({"portVersion": 2, "kind": "ShellControl", "payload": {
            "parentPid": shell.parent_pid, "generation": state["generation"], "ownerEpoch": state["owner_epoch"],
            "requestId": str(uuid4()), "approvalHash": "c" * 64, "phase": "accepted"}},
            command,
            {"portVersion": 2, "kind": "AutomationState", "payload": {
                "paused": False, "cancelled": False, "metadataHealthy": True, "approvalValid": True}})
        self.until(lambda: self.pane.state["lifecycle"]["main_exit"] is not None)
        shell.release_input()
        self.until(lambda: self.pane.state["lifecycle"]["controller_exit"] is not None)
        return self.pane.state["lifecycle"]

    def assert_managed_ok(self):
        marker = self.home / f"managed-{uuid4().hex}"
        life = self.run_managed(f"echo managed-ok > {marker}")
        self.assertEqual(life["controller_exit"], 0, life)
        self.assertIn("RETURN:0", self.pane.shell._transport.events)
        self.assertEqual(marker.read_text(), "managed-ok\n")


class AppliedTests(BashrcCase):
    def test_alias_function_path_and_env_from_the_rc_are_applied(self):
        def tool(home):
            (home / "bin").mkdir()
            (home / "bin" / "wbtool").write_text("#!/bin/sh\necho tool-ok\n")
            (home / "bin" / "wbtool").chmod(0o755)
        self.start("alias wbal='echo alias-ok'\n"
                   "wbfn() { echo fn-ok; }\n"
                   "export RCVAR=rc-ok\n"
                   'export PATH="$HOME/bin:$PATH"\n', setup=tool)
        self.assertEqual(self.output('wbal; wbfn; echo "$RCVAR"; wbtool; echo "ws=$WORKBENCH_SHELL"'),
                         "alias-ok\nfn-ok\nrc-ok\ntool-ok\nws=1\n")
        self.assert_managed_ok()


class StatusTests(BashrcCase):
    def test_applied_rc_output_is_shown_and_the_status_says_applied(self):
        self.start("echo HSB-WELCOME\n")
        self.until(lambda: b"HSB-WELCOME" in self.shown)
        self.assertEqual(self.pane.shell_state()["user_rc"],
                         {"state": "applied", "path": str(self.home / ".bashrc"), "reason": None})

    def test_missing_unreadable_or_directory_rc_starts_normally(self):
        cases = {"missing": lambda home: None,
                 "unreadable": lambda home: ((home / ".bashrc").write_text("export RCVAR=read\n"),
                                             (home / ".bashrc").chmod(0)),
                 "directory": lambda home: (home / ".bashrc").mkdir()}
        for name, setup in cases.items():
            with self.subTest(name):
                if name == "unreadable" and os.geteuid() == 0:
                    continue  # root reads a 0000 file
                self.start(None, setup=setup)
                self.assertEqual(self.output('echo "${RCVAR-unset}:$WORKBENCH_SHELL"'), "unset:1\n")
                self.assertEqual(self.pane.shell_state()["user_rc"],
                                 {"state": "absent", "path": str(self.home / ".bashrc"), "reason": None})
                self.assert_managed_ok()
                self.pane.close()


class StartDirectoryTests(BashrcCase):
    """HSB-D07: an rc ``cd`` does not move where the host shell starts; OLDPWD is the one before the rc."""

    def test_rc_cd_is_undone(self):
        start = os.getcwd()
        self.start("cd /tmp\n")
        self.assertEqual(self.pane.cwd(), os.path.realpath(start))
        self.assertEqual(self.output('echo "$PWD|${OLDPWD-unset}"'), f"{start}|unset\n")
        self.assertEqual(self.pane.shell_state()["user_rc"]["state"], "applied")
        self.assertIsNone(self.pane.shell_state()["user_rc"]["reason"])
        self.assert_managed_ok()

    def test_deleted_start_directory_keeps_the_rc_directory_with_a_note(self):
        start = os.getcwd()
        gone = tempfile.mkdtemp(prefix="hsb-gone-")
        os.chdir(gone)
        try:
            os.rmdir(gone)
            self.start("cd /tmp\n")
        finally:
            os.chdir(start)
        self.assertEqual(self.pane.cwd(), "/tmp")
        state = self.pane.shell_state()["user_rc"]
        self.assertEqual(state["state"], "applied", state)
        self.assertIn("시작 디렉터리", state["reason"] or "")
        self.assert_managed_ok()


class ShKindTests(BashrcCase):
    def test_sh_host_shell_is_unchanged(self):
        directory = tempfile.TemporaryDirectory(prefix="hsb-sh-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        (self.home / ".bashrc").write_text("export RCVAR=read\n")
        self.shown = bytearray()
        self.pane = ShellPane(ShellChoice("sh", "/usr/bin/dash"),
                              {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8"})
        self.addCleanup(self.pane.close)
        self.until(lambda: self.pane.state["parent_mode"] == "manual_prompt")
        self.assertIsNone(self.pane.shell_state()["user_rc"])
        self.assertEqual(self.output('echo "${RCVAR-unset}:${WORKBENCH_SHELL-unset}:${ENV-unset}"'),
                         "unset:unset:unset\n")


class PromptTests(BashrcCase):
    """HSB-D02: Workbench's PS1/PROMPT_COMMAND win; the user's prompt hook is not run (no PRIOR_HOOK slot)."""

    def check_prompt_reasserted(self):
        self.assertEqual(self.output('printf "%s|%s|%s" "$PS1" "${#PROMPT_COMMAND[@]}" "$PROMPT_COMMAND"'),
                         "$ |1|__b_emit READY")
        self.type_line("true")
        self.assertFalse((self.home / "pc-ran").exists(), "the rc's PROMPT_COMMAND ran at a prompt")
        self.assert_managed_ok()

    def test_ps1_theme_and_prompt_command_string(self):
        self.start("PS1='\\[\\e[32m\\]\\u@\\h:\\w\\$\\[\\e[0m\\] '\n"
                   "PROMPT_COMMAND='touch \"$HOME/pc-ran\"; history -a'\n")
        self.check_prompt_reasserted()

    def test_prompt_command_array(self):
        self.start("PROMPT_COMMAND=('history -a' 'touch \"$HOME/pc-ran\"')\n")
        self.check_prompt_reasserted()


class OptionTests(BashrcCase):
    """HSB-D02: options that break the controller or make it noisy are reset after the rc."""

    def test_strict_mode_tracing_and_extdebug_are_reset(self):
        self.start("set -euxo pipefail -o errtrace -o functrace\nshopt -s extdebug\n")
        flags = self.output('printf "%s" "$-"')
        self.assertFalse(set(flags) & set("euxET"), flags)
        self.assertEqual(self.output("shopt -o pipefail; shopt extdebug").split(),
                         ["pipefail", "off", "extdebug", "off"])
        self.type_line("false")  # set -e would end an interactive shell here
        self.type_line("echo $UNSET_NAME_HSB")  # set -u would abort the line
        self.assertTrue(self.pane.alive())
        self.assert_managed_ok()


class TrapTests(BashrcCase):
    def test_chld_debug_err_return_traps_are_removed_and_others_kept(self):
        self.start("trap 'echo chld' CHLD\ntrap 'touch \"$HOME/debug-ran\"' DEBUG\n"
                   "trap 'echo err' ERR\ntrap 'echo ret' RETURN\ntrap 'echo bye' EXIT\n")
        traps = self.output("trap -p")
        for signal_name in ("CHLD", "DEBUG", "ERR", "RETURN"):
            self.assertNotIn(signal_name, traps)
        self.assertIn("echo bye", traps)
        (self.home / "debug-ran").unlink(missing_ok=True)
        self.type_line("true")
        self.assertFalse((self.home / "debug-ran").exists())
        self.assert_managed_ok()  # a CHLD trap would reject the handoff (HOOK_REJECTED)


class ControlFdTests(BashrcCase):
    """HSB-D04: the rc runs with fds 7/8/9 closed; whatever it does to them is undone after it."""

    def test_rc_that_closes_or_reopens_the_control_fds_is_harmless(self):
        self.start('exec 9>&-\nexec 8<&-\nexec 7>/dev/null\nexec 9>"$HOME/lock"\n')
        self.assert_managed_ok()

    def test_a_program_the_rc_leaves_running_holds_no_control_fd(self):
        self.start('sleep 7311 & echo $! > "$HOME/bg.pid"; disown\n')
        pid = int((self.home / "bg.pid").read_text())
        self.addCleanup(lambda: os.kill(pid, 9) if Path(f"/proc/{pid}").exists() else None)
        targets = {fd: os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")}
        self.assertFalse({"7", "8", "9"} & set(targets), targets)
        self.assertFalse([t for fd, t in targets.items() if fd not in {"0", "1", "2"}], targets)
        self.assert_managed_ok()


class FallbackTests(BashrcCase):
    """HSB-D05: an rc that does not finish or leaves Workbench's hooks broken is dropped: the host shell is
    started again without it, with a one-line reason in the pane and in the shell status."""

    BOUND = 10.0

    def assert_fallback(self, reason_part: str):
        state = self.pane.shell_state()["user_rc"]
        self.assertEqual((state["state"], state["path"]), ("fallback", str(self.home / ".bashrc")), state)
        self.assertIn(reason_part, state["reason"])
        self.until(lambda: "~/.bashrc 적용 실패:".encode() in self.shown)
        line = bytes(self.shown).split("~/.bashrc 적용 실패:".encode(), 1)[1].split(b"\n", 1)[0].decode()
        self.assertIn(state["reason"], line)
        self.assertIn("기본 설정으로 시작", line)
        # the new shell runs without the rc and is a normal Workbench host shell
        self.assertEqual(self.output('echo "${RCVAR-unset}:$WORKBENCH_SHELL"'), "unset:1\n")
        self.assert_managed_ok()

    def test_rc_that_replaces_or_ends_the_shell_falls_back_at_once(self):
        for name, rc, reason in (("exec", "export RCVAR=x\nexec sh\n", "exec"),
                                 ("exit", "export RCVAR=x\nexit 4\n", "종료")):
            with self.subTest(name):
                self.start(rc)
                self.assertLess(self.startup_seconds, 5.0)
                self.assert_fallback(reason)
                self.pane.close()

    def test_rc_that_blocks_falls_back_within_the_bound(self):
        for name, rc in (("read", "export RCVAR=x\nread -r answer\n"), ("sleep", "export RCVAR=x\nsleep 60\n")):
            with self.subTest(name):
                self.start(rc)
                self.assertGreaterEqual(self.startup_seconds, self.BOUND - 0.5)
                self.assertLess(self.startup_seconds, self.BOUND + 4.0)
                self.assert_fallback(f"{self.BOUND:g}초")
                self.pane.close()

    def test_rc_that_pins_the_prompt_or_overrides_a_controller_builtin_falls_back(self):
        for name, rc, reason in (("readonly PS1", "export RCVAR=x\nreadonly PS1='> '\n", "PS1"),
                                 ("readonly PROMPT_COMMAND",
                                  "export RCVAR=x\nreadonly PROMPT_COMMAND='history -a'\n", "PROMPT_COMMAND"),
                                 # review fix 2: a printf() override no longer breaks the controller (it calls
                                 # ``builtin printf``); only ``builtin`` itself cannot be protected
                                 ("builtin function", "export RCVAR=x\nbuiltin() { :; }\n", "builtin"),
                                 # review fix 4: a disabled builtin the controller needs is named, never "10초"
                                 ("enable -n trap", "export RCVAR=x\nenable -n trap\n", "enable -n trap"),
                                 ("enable -n echo", "export RCVAR=x\nenable -n echo\n", "enable -n echo")):
            with self.subTest(name):
                self.start(rc)
                self.assertLess(self.startup_seconds, 5.0)
                self.assert_fallback(reason)
                self.pane.close()


FAKE_CONDA = r'''#!/bin/sh
# offline stand-in for "conda shell.bash hook": functions, PATH, an activated base env and its prompt prefix
cat <<'HOOK'
export CONDA_EXE="$HOME/conda/bin/conda"
__conda_activate() {
    \export CONDA_DEFAULT_ENV="$1" CONDA_PREFIX="$HOME/conda" CONDA_SHLVL=1
    \export PATH="$HOME/conda/bin:$PATH"
    PS1="($1) ${PS1:-}"
}
conda() {
    \local cmd="${1-__missing__}"
    case "$cmd" in
        activate|deactivate) __conda_activate "${2-base}" ;;
        *) \command "$CONDA_EXE" "$@" ;;
    esac
}
conda activate base
HOOK
'''

CONDA_RC = '''# >>> conda initialize >>>
__conda_setup="$("$HOME/conda/bin/conda" 'shell.bash' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
else
    export PATH="$HOME/conda/bin:$PATH"
fi
unset __conda_setup
# <<< conda initialize <<<
'''


class CondaTests(BashrcCase):
    def test_conda_style_init_is_applied_without_its_prompt_prefix(self):
        def conda(home):
            (home / "conda" / "bin").mkdir(parents=True)
            (home / "conda" / "bin" / "conda").write_text(FAKE_CONDA)
            (home / "conda" / "bin" / "conda").chmod(0o755)
        self.start(CONDA_RC, setup=conda)
        self.assertEqual(self.pane.shell_state()["user_rc"]["state"], "applied")
        self.assertEqual(self.output('type -t conda; echo "$CONDA_DEFAULT_ENV|${PATH%%:*}|$PS1"'),
                         f"function\nbase|{self.home}/conda/bin|$ \n")
        self.assert_managed_ok()


class HardeningTests(BashrcCase):
    """Review fixes 1-3: noclobber, user functions/aliases named like the controller's builtins, allexport."""

    def take_back(self):
        self.pane.state = self.pane.shell.request_takeover()
        self.until(lambda: self.pane.state["parent_mode"] == "manual_prompt")

    def test_noclobber_keeps_every_handoff_trap_list_current(self):
        self.start("set -o noclobber\n")
        self.assert_managed_ok()
        self.take_back()
        self.type_line("set -C; trap 'echo c' CHLD")  # noclobber on again, then a CHLD trap: must be rejected
        before = len(self.pane.shell._transport.events)
        self.assertIsNone(self.pane.admit(b"wb-handoff\r"))
        self.until(lambda: "HOOK_REJECTED" in self.pane.shell._transport.events[before:])
        self.assertNotIn(b"cannot overwrite existing file", bytes(self.shown))

    def test_functions_and_aliases_named_like_controller_builtins_do_not_defeat_the_reset(self):
        self.start("set -e -o pipefail\ntrap 'echo c' CHLD\n"
                   "trap() { :; }\nunset() { :; }\nprintf() { :; }\ncat() { :; }\nread() { :; }\n"
                   "jobs() { :; }\neval() { :; }\ncompgen() { :; }\ntest() { :; }\n"
                   "alias set=:\nalias shopt=:\nalias builtin=:\n")
        self.assertEqual(self.pane.shell_state()["user_rc"]["state"], "applied")
        traps, flags, pipefail, kind = self.output(
            # the user's ``alias builtin=:`` stays active at the prompt: ``\\builtin`` bypasses it here
            "\\builtin trap -p CHLD | \\builtin command wc -l; \\builtin printf '%s\\n' \"$-\"; "
            "\\builtin shopt -o pipefail; type -t trap").split("\n")[:4]
        self.assertEqual(traps.strip(), "0")
        self.assertNotIn("e", flags)
        self.assertEqual(pipefail.split(), ["pipefail", "off"])
        self.assertEqual(kind, "function")  # the user's own function is left in place
        self.assert_managed_ok()

    def test_allexport_does_not_export_control_variables_and_stays_off(self):
        self.start("set -a\nRCVAR=exported\n")
        self.assertEqual(self.output("env | grep -E '^(BOUNDARY_|__b_)' | cut -d= -f1; "
                                     "case $- in *a*) echo allexport-on;; esac; env | grep ^RCVAR="),
                         "RCVAR=exported\n")
        marker = self.home / "managed-env"
        life = self.run_managed(f"env | grep -E '^(BOUNDARY_|__b_)' | cut -d= -f1 > {marker}; true")
        self.assertEqual(life["controller_exit"], 0, life)
        self.assertEqual(marker.read_text(), "")


class AliasTests(BashrcCase):
    def test_user_aliases_do_not_rewrite_the_controller_but_stay_for_the_user(self):
        self.start("alias printf='echo HIJACK'\nalias cat='echo HIJACK'\nalias jobs='echo HIJACK'\n"
                   "alias trap='echo HIJACK'\nalias read='echo HIJACK'\n")
        self.assertEqual(self.output("cat /dev/null"), "HIJACK /dev/null\n")
        self.assert_managed_ok()


class ReadlineTests(BashrcCase):
    """HSB-D03: ``set -o vi`` is kept. vi command mode is entered with ESC, which the boundary already treats
    as an unknown edit (held until the next READY), so a vi-edited line, its completion and a later
    wb-handoff/managed request behave as in emacs mode."""

    def test_vi_mode_is_kept_and_a_vi_edited_line_completes(self):
        self.start("set -o vi\n")
        self.assertEqual(self.output("set -o | grep -E '^(vi|emacs) ' | tr -s ' \\t' ' '"), "emacs off\nvi on\n")
        out = self.home / "vi-out"
        before = len(self.pane.shell._transport.events)
        # insert "...-x", ESC to command mode, x deletes the x, A appends: the shell runs "echo hsb-vi-ok"
        self.assertIsNone(self.pane.admit(b"echo hsb-vi-x\x1bxAok > " + str(out).encode() + b"\r"))
        self.until(lambda: "READY" in self.pane.shell._transport.events[before:]
                   and self.pane.state["parent_mode"] == "manual_prompt")
        self.assertEqual(out.read_text(), "hsb-vi-ok\n")
        self.assert_managed_ok()


if __name__ == "__main__":
    unittest.main()
