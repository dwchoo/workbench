"""HB-11 final integration of the Workbench Hub against real backends (fake OMP), CLI subprocesses and ptys.

Independent: the expected values are the BRIEF HB-AC-01..19 sentences and the SPEC tables (field mapping, exit
codes, texts), read back from ``/proc`` (pid, start ticks, ``/proc/locks``), the project snapshot
(``wb status --json``), registry/``.wb`` bytes and files the test wrote itself; never the product's identity or
mapping helpers. The harness is ``hub_final_support`` (temp HOME/XDG, short private ``XDG_RUNTIME_DIR``, placeholder
``agent.db``, fake OMP; real ``systemctl`` is never reachable: a fake one leads ``PATH``). Items that need the target
host (real reboot, real reinstall with a new version, real systemd/linger) are NOT here and are reported ``pending``.
Run with ``discover -s tests/integration -p 'live_hub_e2e_independent.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hub_final_support import (  # noqa: E402
    DETACH, REPO, HubBox, Terminal, alive, kill_exact, parent_and_session, start_ticks, wait_until)

MANAGED_TEXT = ("[workbench] WB가 관리하는 terminal 안에서는 Hub를 자동으로 시작하지 않습니다. "
                "외부 terminal에서 'wb hub start'를 실행하세요.")
NOT_RUNNING = 'Hub 실행 중 아님'
UPDATE_DRIVER = ("import os, pathlib, sys\n"
                 "from workbench.backend import maintenance\n"
                 "maintenance.FENCE_BASE = pathlib.Path(os.environ.pop('WB_TEST_FENCE_BASE'))\n"
                 "from workbench.backend import cli\n"
                 "raise SystemExit(cli.main(['update']))\n")


class Case(unittest.TestCase):
    autostart = '0'

    def setUp(self):
        self.box = HubBox(self, autostart=self.autostart)
        self.plain = self.box.folder('plain')

    def hub_state_clean(self) -> None:
        """No Hub process, no lock holder, no record: what `Hub 없음` means for HB-AC-15."""
        self.assertEqual(self.box.hubs(), {})
        self.assertIsNone(self.box.hub_lock_holder())
        self.assertFalse((self.box.hub_state / 'hub.json').exists())

    def register_project(self, name: str) -> tuple[Path, str, dict]:
        root, project_id = self.box.project(name)
        return root, project_id, self.box.entry(root, project_id)

    def attach_direct(self, term: Terminal, root: Path) -> None:
        """The product UI is up for ``root`` (the backend sees an attached UI)."""
        def attached() -> bool:
            term.pump(0.1)
            snapshot = self.box.status(root)
            return bool(snapshot and snapshot.get('attached'))
        self.assertTrue(wait_until(attached, 120), 'never attached; screen:\n' + term.text())


# ---------------------------------------------------------------------------------------------------------
class HubStartTests(Case):
    autostart = '1'

    def test_ac01_a_wb_outside_a_project_starts_a_hub_that_outlives_its_terminal(self):
        self.hub_state_clean()
        term = Terminal(self, self.box.default_env(), cwd=self.plain)
        term.wait_for('Workbench Hub', 40)
        hubs = self.box.hubs()
        self.assertEqual(len(hubs), 1, hubs)
        (hub_pid, hub_ticks), = hubs.items()
        self.assertEqual(self.box.hub_lock_holder(), hub_pid)
        record = json.loads((self.box.hub_state / 'hub.json').read_text())
        self.assertEqual((record['pid'], record['start_ticks'], record['mode']), (hub_pid, hub_ticks, 'spawned'))
        self.assertNotEqual(parent_and_session(hub_pid)[1], parent_and_session(term.pid)[1])  # its own session
        term.close()  # the terminal goes away
        time.sleep(1.0)
        self.assertTrue(alive(hub_pid, hub_ticks))
        self.assertEqual(self.box.hub_lock_holder(), hub_pid)
        self.assertEqual(self.box.wb_env('hub', 'status', env=self.box.quiet()).returncode, 0)

    def test_ac01_eight_simultaneous_wb_leave_one_hub_and_every_caller_sees_it(self):
        self.hub_state_clean()
        callers = [subprocess.Popen([sys.executable, '-m', 'workbench', 'hub', 'list', '--json'], env=self.box.default_env(),
                                    cwd=self.plain, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True) for _ in range(8)]
        self.box.processes.extend(callers)
        results = [caller.communicate(timeout=120) + (caller.returncode,) for caller in callers]
        for out, err, code in results:
            self.assertEqual(code, 0, out + err)
            self.assertEqual(json.loads(out)['rows'], [])
        hubs = self.box.hubs()
        self.assertEqual(len(hubs), 1, hubs)
        (hub_pid, _ticks), = hubs.items()
        self.assertEqual(self.box.hub_lock_holder(), hub_pid)
        record = json.loads((self.box.hub_state / 'hub.json').read_text())
        self.assertEqual(record['pid'], hub_pid)
        again = self.box.wb_env('hub', 'start', env=self.box.quiet())
        self.assertEqual(again.returncode, 0, again)
        self.assertIn(f'pid {hub_pid}', again.stdout)  # `wb hub start` finds the same Hub
        self.assertEqual(list(self.box.hubs()), [hub_pid])


class HubExceptionCommandTests(Case):
    """HB-AC-15 / HB-T08 / HB-AC-04: the listed commands never start a Hub even with auto-start on."""
    autostart = '1'

    def fake_bin(self) -> Path:
        bin_dir = self.box.base / 'fakebin'
        bin_dir.mkdir()
        log = self.box.base / 'systemctl.log'
        script = ('#!/bin/sh\n'
                  f'printf \'%s %s\\n\' "$(basename "$0")" "$*" >> {shlex.quote(str(log))}\n'
                  'case "$1 $2" in "--user is-enabled") if [ -e "$XDG_CONFIG_HOME/systemd/user/omp-workbench-hub.service" ];'
                  ' then echo enabled; exit 0; else echo disabled; exit 1; fi;; esac\n'
                  'case "$*" in *"show-user"*) echo Linger=no;; esac\n'
                  'exit 0\n')
        for name in ('systemctl', 'loginctl'):
            (bin_dir / name).write_text(script)
            (bin_dir / name).chmod(0o700)
        self.systemctl_log = log
        return bin_dir

    def test_hub_free_commands_start_no_hub(self):
        self.hub_state_clean()
        fake = self.fake_bin()
        env = self.box.env_with(PATH=f'{fake}:/usr/bin:/bin')
        cases = [(('hub', 'status'), 3, NOT_RUNNING), (('hub', 'stop'), 3, NOT_RUNNING),
                 (('--version',), 0, 'wb '), (('--help',), 0, 'usage'), (('hub', 'passwd'), 2, ''),
                 (('hub', 'tls'), 2, ''), (('hub', 'enable'), 0, ''), (('hub', 'disable'), 0, ''),
                 (('hub', 'status', '--json'), 3, 'not_running')]
        for args, code, needle in cases:
            with self.subTest(args=args):
                done = self.box.wb_env(*args, cwd=self.plain, env=env, timeout=60)
                self.assertEqual(done.returncode, code, done.stdout + done.stderr)
                self.assertIn(needle, done.stdout + done.stderr)
                self.hub_state_clean()
        self.assertFalse(self.box.hub_state.exists() and (self.box.hub_state / 'hub.lock').exists())

    def test_setup_doctor_update_start_no_hub(self):
        self.hub_state_clean()
        for args in (('doctor',), ('setup',), ('update',)):
            with self.subTest(args=args):
                done = self.box.wb_env(*args, cwd=self.plain, timeout=120)  # no tty, no install: refusals are fine
                self.assertNotIn('Hub를 시작했습니다', done.stdout + done.stderr)
                self.hub_state_clean()

    def test_ac04_default_has_no_unit_enable_and_disable_toggle_it_and_a_failed_enable_leaves_none(self):
        fake = self.fake_bin()
        env = self.box.env_with(PATH=f'{fake}:/usr/bin:/bin')
        unit = self.box.base / 'home' / '.config' / 'systemd' / 'user' / 'omp-workbench-hub.service'
        self.assertFalse(unit.exists(), 'nothing creates a unit by default')
        self.assertEqual(self.box.wb_env('hub', 'start', env=env).returncode, 0)
        self.assertFalse(unit.exists())
        self.assertFalse(self.systemctl_log.exists(), 'starting a Hub never calls systemctl')
        (hub_pid, hub_ticks), = self.box.hubs().items()
        done = self.box.wb_env('hub', 'enable', env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        text = unit.read_text()
        for needle in ('Type=simple', 'Restart=on-failure', 'RestartSec=5', 'WantedBy=default.target',
                       '_hub', '--foreground', 'systemd'):
            self.assertIn(needle, text)
        calls = self.systemctl_log.read_text().splitlines()
        self.assertIn('systemctl --user daemon-reload', calls)
        self.assertIn('systemctl --user enable omp-workbench-hub.service', calls)
        self.assertLess(calls.index('systemctl --user daemon-reload'),
                        calls.index('systemctl --user enable omp-workbench-hub.service'))
        self.assertIn('linger', done.stdout.lower())
        self.assertTrue(alive(hub_pid, hub_ticks), 'enable must not touch the running Hub')
        status = self.box.wb_env('hub', 'status', '--json', env=env)
        self.assertEqual(json.loads(status.stdout)['boot_autostart'], 'enabled')
        done = self.box.wb_env('hub', 'disable', env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(unit.exists())
        self.assertIn('systemctl --user disable omp-workbench-hub.service', self.systemctl_log.read_text())
        self.assertTrue(alive(hub_pid, hub_ticks), 'disable must not stop the running Hub')
        # a failing `systemctl enable` leaves no unit behind and says why
        failing = self.box.base / 'failbin'
        failing.mkdir()
        (failing / 'systemctl').write_text('#!/bin/sh\ncase "$*" in *enable*) echo "boom enable" >&2; exit 1;; esac\nexit 0\n')
        (failing / 'systemctl').chmod(0o700)
        (failing / 'loginctl').write_text('#!/bin/sh\necho Linger=no\n')
        (failing / 'loginctl').chmod(0o700)
        done = self.box.wb_env('hub', 'enable', env=self.box.env_with(PATH=f'{failing}:/usr/bin:/bin'))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('boom enable', done.stderr)
        self.assertFalse(unit.exists())
        self.assertEqual(self.box.wb_env('hub', 'stop', '--yes', env=env).returncode, 0)

    def test_version_text_and_exit_without_hub(self):
        done = self.box.wb_env('--version', cwd=self.plain)
        self.assertEqual(done.returncode, 0)
        self.assertRegex(done.stdout.strip(), r'^wb \S+ \((\S{12}|editable|unknown)\)$')
        self.hub_state_clean()


# ---------------------------------------------------------------------------------------------------------
class RegistryTests(Case):
    """HB-AC-05/06/07/14/17 with real backends (the registry is the CLI's own file, read back as bytes)."""
    autostart = '1'

    def registry(self) -> list[dict]:
        data = self.box.registry_bytes()
        return json.loads(data)['projects'] if data else []

    def rows(self) -> dict[str, dict]:
        done = self.box.wb_env('hub', 'list', '--json', cwd=self.plain)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return {row['root']: row for row in json.loads(done.stdout)['rows']}

    def test_ac05_init_start_and_add_register_and_ac06_unregistered_wb_is_not_listed(self):
        gamma, gamma_id, _entry = self.register_project('gamma')  # initialised, never started, never registered
        alpha, alpha_id, _entry = self.register_project('alpha')
        # first start of an existing project registers it (source "start"), a second start does not change it
        for _ in range(2):
            done = self.box.wb_env('start', '--no-attach', cwd=alpha, env=self.box.quiet(), timeout=240)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        entries = [item for item in self.registry() if item['root'] == str(alpha)]
        self.assertEqual([(item['project_id'], item['source']) for item in entries], [(alpha_id, 'start')])
        # wb init on a pty (the confirmation is the user's)
        fresh = self.box.folder('fresh')
        term = Terminal(self, self.box.quiet(), 'init', str(fresh), cwd=self.plain)
        term.wait_for('[y/N]', 30)
        self.assertFalse((fresh / '.wb').exists(), 'nothing is created before the answer')
        term.send(b'y\r')
        self.assertEqual(term.wait_exit(), 0, term.text())
        fresh_id = json.loads((fresh / '.wb' / 'project.json').read_text())['project_id']
        # wb hub add of a project initialised elsewhere
        beta, beta_id, _entry = self.register_project('beta')
        done = self.box.wb_env('hub', 'add', str(beta), env=self.box.quiet())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        by_root = {item['root']: item for item in self.registry()}
        self.assertEqual({root: (item['project_id'], item['source']) for root, item in by_root.items()},
                         {str(alpha): (alpha_id, 'start'), str(fresh): (fresh_id, 'init'),
                          str(beta): (beta_id, 'add')})
        rows = self.rows()  # gamma has a `.wb` but was neither registered nor started: not listed
        self.assertEqual(set(rows), {str(alpha), str(fresh), str(beta)})
        self.assertNotIn(str(gamma), rows)
        self.assertEqual((rows[str(alpha)]['power'], rows[str(fresh)]['power'], rows[str(beta)]['power']),
                         ('on', 'off', 'off'))
        done = self.box.wb_env('hub', 'add', str(beta), env=self.box.quiet())  # idempotent, no duplicate
        self.assertEqual(done.returncode, 0)
        self.assertEqual(sum(item['root'] == str(beta) for item in self.registry()), 1)

    def test_ac07_remove_keeps_the_wb_bytes_and_a_re_add_continues_the_same_project(self):
        alpha, alpha_id, entry = self.register_project('alpha')
        self.box.register(entry)
        self.box.power_on(alpha, alpha_id)
        before = self.box.registry_bytes()
        # AC-17: a running project is not removed ("먼저 끄세요"), the registry stays byte-identical
        done = self.box.wb_env('hub', 'remove', str(alpha), env=self.box.quiet())
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('먼저 끄세요', done.stdout + done.stderr)
        self.assertEqual(self.box.registry_bytes(), before)
        done = self.box.wb_env('shutdown', '--yes', cwd=alpha, env=self.box.quiet(), timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(wait_until(lambda: self.box.backends() == {}, 30))
        project_bytes = self.box.tree_bytes(alpha)
        self.assertIn('.wb/project.json', project_bytes)
        done = self.box.wb_env('hub', 'remove', str(alpha), env=self.box.quiet())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.registry(), [])
        self.assertEqual(self.box.tree_bytes(alpha), project_bytes, 'unregistering must not touch .wb')
        done = self.box.wb_env('hub', 'add', str(alpha), env=self.box.quiet())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        again = [item for item in self.registry() if item['root'] == str(alpha)]
        self.assertEqual([item['project_id'] for item in again], [alpha_id])
        self.assertEqual(self.box.tree_bytes(alpha), project_bytes)

    def test_ac17_adding_an_uninitialised_folder_is_refused_with_init_guidance_and_creates_nothing(self):
        bare = self.box.folder('bare')
        done = self.box.wb_env('hub', 'add', str(bare), env=self.box.quiet())
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('wb init', done.stdout + done.stderr)
        self.assertFalse((bare / '.wb').exists())
        self.assertEqual(self.registry(), [])

    def test_ac14_nested_and_duplicate_registrations_are_refused(self):
        alpha, alpha_id, entry = self.register_project('alpha')
        self.assertEqual(self.box.wb_env('hub', 'add', str(alpha), env=self.box.quiet()).returncode, 0)
        sub = alpha / 'sub'
        sub.mkdir()
        before = self.box.registry_bytes()
        done = self.box.wb_env('hub', 'add', str(sub), env=self.box.quiet())
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(str(alpha), done.stdout + done.stderr)  # names the real root
        self.assertEqual(self.box.registry_bytes(), before)
        term = Terminal(self, self.box.quiet(), 'init', str(sub), cwd=self.plain)  # nested init
        term.pump(2.0)
        if '[y/N]' in term.text():
            term.send(b'y\r')
        self.assertEqual(term.wait_exit(), 1, term.text())
        self.assertFalse((sub / '.wb').exists(), 'no nested initialisation')
        self.assertEqual(self.box.registry_bytes(), before)
        self.assertEqual(len([item for item in self.registry() if item['project_id'] == alpha_id]), 1)


# ---------------------------------------------------------------------------------------------------------
class BackendIndependenceTests(Case):
    """HB-AC-02/03/08/11: the backend lives on its own; a Hub that is stopped or killed changes nothing."""
    autostart = '1'

    def setUp(self):
        super().setUp()
        self.alpha, self.alpha_id, self.entry = self.register_project('alpha')
        self.box.register(self.entry)

    def test_ac08_power_on_twice_makes_one_backend_and_ac02_hub_stop_keeps_it_listed_on(self):
        pid, ticks = self.box.power_on(self.alpha, self.alpha_id)
        self.assertEqual(self.box.power_on(self.alpha, self.alpha_id), (pid, ticks), 'no second backend')
        self.assertEqual(self.box.backends(), {pid: ticks})
        self.assertEqual(self.box.lock_holders(self.alpha), {pid})
        hub_pid, hub_ticks = self.box.start_hub()
        panes = self.box.pane_processes(self.box.status(self.alpha))
        self.assertEqual(self.box.hub_row(self.alpha)['power'], 'on')
        done = self.box.wb_env('hub', 'stop', env=self.box.quiet(), timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('켜져 있던 WB 프로젝트는 계속 실행됩니다', done.stdout)
        self.assertTrue(wait_until(lambda: not alive(hub_pid, hub_ticks), 15))
        time.sleep(1.0)
        self.assertTrue(alive(pid, ticks))
        self.assertTrue(all(alive(*ref) for ref in panes.values()), panes)
        # the next `wb` that needs a Hub starts a new one whose list says the project is on
        done = self.box.wb_env('hub', 'list', '--json', cwd=self.plain)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        rows = json.loads(done.stdout)['rows']
        self.assertEqual([(row['root'], row['power'], row['backend']) for row in rows],
                         [(str(self.alpha), 'on', {'pid': pid, 'start_ticks': ticks})])
        new_hub = self.box.hubs()
        self.assertEqual(len(new_hub), 1)
        self.assertNotIn(hub_pid, new_hub)

    def test_ac03_a_killed_hub_leaves_the_project_and_a_wb_in_it_connects_directly(self):
        pid, ticks = self.box.power_on(self.alpha, self.alpha_id)
        hub_pid, hub_ticks = self.box.start_hub()
        panes = self.box.pane_processes(self.box.status(self.alpha))
        self.assertTrue(kill_exact(hub_pid, hub_ticks))
        self.assertTrue(wait_until(lambda: not alive(hub_pid, hub_ticks), 15))
        time.sleep(1.0)
        self.assertTrue(alive(pid, ticks) and all(alive(*ref) for ref in panes.values()))
        # no Hub, auto-start off: bare `wb` in the project goes straight to the backend
        term = Terminal(self, self.box.quiet(), cwd=self.alpha)
        self.attach_direct(term, self.alpha)
        self.assertEqual(self.box.hubs(), {}, 'a direct connection needs no Hub')
        term.send(DETACH)
        self.assertEqual(term.wait_exit(), 0, term.text())
        self.assertIn('detached', term.text())
        self.assertTrue(alive(pid, ticks))
        snapshot = self.box.status(self.alpha)
        self.assertEqual((snapshot['phase'], snapshot['attached']), ('ready', False))
        done = self.box.wb_env('hub', 'status', env=self.box.quiet())  # the dead Hub's record is stale, not a Hub
        self.assertEqual((done.returncode, done.stdout.strip()), (3, NOT_RUNNING))

    def test_ac03_after_wb_hub_stop_a_wb_in_the_project_also_connects_directly(self):
        pid, ticks = self.box.power_on(self.alpha, self.alpha_id)
        self.box.start_hub()
        self.assertEqual(self.box.wb_env('hub', 'stop', env=self.box.quiet(), timeout=60).returncode, 0)
        term = Terminal(self, self.box.quiet(), cwd=self.alpha)
        self.attach_direct(term, self.alpha)
        term.send(DETACH)
        self.assertEqual(term.wait_exit(), 0, term.text())
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.hubs(), {})

    def test_ac11_a_wb_in_a_subfolder_connects_and_starts_the_hub(self):
        pid, ticks = self.box.power_on(self.alpha, self.alpha_id)
        self.hub_state_clean()
        sub = self.alpha / 'deep' / 'er'
        sub.mkdir(parents=True)
        term = Terminal(self, self.box.default_env(), cwd=sub)  # auto-start on
        self.attach_direct(term, self.alpha)
        self.assertTrue(wait_until(lambda: len(self.box.hubs()) == 1, 30))
        term.send(DETACH)
        self.assertEqual(term.wait_exit(), 0, term.text())
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.backends(), {pid: ticks})
        self.assertEqual(self.box.hub_row(self.alpha)['power'], 'on')


# ---------------------------------------------------------------------------------------------------------
class HostShellTests(Case):
    """HB-T01 with a real host shell: a `wb` typed into a WB-managed pane never starts a Hub."""

    def setUp(self):
        super().setUp()
        self.alpha, self.alpha_id, self.entry = self.register_project('alpha')
        self.box.register(self.entry)
        self.box.power_on(self.alpha, self.alpha_id)
        self.client = self.box.attach(self.alpha)
        self.n = 0

    def in_pane(self, *args: str, timeout: float = 90) -> tuple[int, str]:
        """Type ``wb ARGS`` into the host shell; its exit code and combined output."""
        self.n += 1
        out, rc = self.box.base / f'pane-{self.n}.out', self.box.base / f'pane-{self.n}.rc'
        environment = ' '.join(f'{key}={shlex.quote(self.box.env[key])}' for key in (
            'HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE'))
        command = (f'env -u WORKBENCH_DATA_DIR {environment} PATH=/usr/bin:/bin WORKBENCH_HUB_AUTOSTART=1 '
                   f'{shlex.quote(sys.executable)} -m workbench {" ".join(map(shlex.quote, args))} '
                   f'> {shlex.quote(str(out))} 2>&1; echo $? > {shlex.quote(str(rc))}\n')
        self.box.type_shell(self.client, command)
        self.assertTrue(wait_until(lambda: (self.client.pump(0.1), rc.exists() and rc.read_text().strip() != '')[1],
                                   timeout), f'pane command never finished: {args}')
        return int(rc.read_text()), out.read_text()

    def test_hb_t01_wb_hub_start_in_the_host_shell_is_refused_and_a_hub_started_outside_is_used(self):
        self.hub_state_clean()
        code, output = self.in_pane('hub', 'start')
        self.assertEqual((code, MANAGED_TEXT in output), (2, True), output)
        self.hub_state_clean()
        code, output = self.in_pane('hub', 'status')  # no Hub, and asking does not start one
        self.assertEqual((code, NOT_RUNNING in output), (3, True), output)
        self.hub_state_clean()
        # auto-start (a project command with auto-start on) is also held back inside the pane
        code, _output = self.in_pane('status', '--json')
        self.assertEqual(code, 0)
        self.hub_state_clean()
        hub_pid, _ticks = self.box.start_hub()  # started from outside
        code, output = self.in_pane('hub', 'status')
        self.assertEqual(code, 0, output)
        self.assertIn(f'pid {hub_pid}', output)
        code, output = self.in_pane('hub', 'list', '--json')  # the pane may use the running Hub
        self.assertEqual(code, 0, output)
        self.assertEqual([row['root'] for row in json.loads(output)['rows']], [str(self.alpha)])
        self.assertEqual(list(self.box.hubs()), [hub_pid])


# ---------------------------------------------------------------------------------------------------------
class RowFieldDriftTests(Case):
    """HB-AC-12/19: the row of a real backend against its own snapshot (SPEC "상태 집계" mapping)."""
    autostart = '1'

    def setUp(self):
        super().setUp()
        self.alpha, self.alpha_id, self.entry = self.register_project('alpha')
        self.box.register(self.entry)
        self.pid, self.ticks = self.box.power_on(self.alpha, self.alpha_id)
        self.box.start_hub()

    def row(self) -> dict:
        time.sleep(2.2)  # the Hub caches a list for 2 s
        return self.box.hub_row(self.alpha)

    def test_the_idle_row_matches_the_snapshot_and_an_empty_inbox_counts_zero(self):
        snapshot = self.box.status(self.alpha)
        row = self.row()
        self.assertEqual((row['name'], row['root'], row['project_id']), ('alpha', str(self.alpha), self.alpha_id))
        self.assertEqual((row['power'], row['backend']), ('on', {'pid': self.pid, 'start_ticks': self.ticks}))
        self.assertEqual(row['task'] is None, snapshot.get('task') is None)
        self.assertIsNone((snapshot['panes']['host_shell']).get('command'))
        self.assertIsNone(row['command'])
        self.assertEqual(row['waits'], [])
        # TI-10 (SPEC "Hub 목록 연결"): a readable (empty) inbox is counts of 0 and mode auto, not null
        self.assertEqual(row['pending_requests'], {'new': 0, 'reviewing': 0, 'needs_info': 0, 'ready': 0, 'running': 0})
        self.assertEqual(row['review_mode'], 'auto')
        self.assertIsInstance(row['last_activity'], (int, float))
        table = self.box.wb_env('hub', 'list', cwd=self.plain)
        self.assertEqual(table.returncode, 0, table)
        line = [item for item in table.stdout.splitlines() if 'alpha' in item][0]
        self.assertIn('켜짐', line)
        cells = [cell for cell in re.split(r'\s{2,}', line.strip())]
        self.assertEqual(cells[6], '0', 'the 요청 column of an empty inbox is 0')

    def test_user_work_and_a_pause_show_in_the_row_as_the_snapshot_has_them(self):
        """A job the user typed into the host shell is not a Task command: the row keeps ``command`` null, as the
        snapshot does (a non-null ``command``/``task`` needs a Task made by a manager OMP, which the fake OMP never
        makes; that mapping is fixture evidence in ``tests/hub/test_hub_list*.py``)."""
        self.box.attach_client = self.box.attach(self.alpha)
        sleeper = self.box.sleeper(self.alpha, 300)
        snapshot = self.box.status(self.alpha)
        self.assertIsNone(snapshot['panes']['host_shell'].get('command'))
        row = self.row()
        self.assertEqual((row['power'], row['command'], row['waits']), ('on', None, []))
        outcome = self.box.drive('pause', self.entry)
        self.assertTrue(outcome['ok'], outcome)
        self.assertIs(self.box.status(self.alpha)['automation']['paused'], True)
        row = self.row()
        self.assertEqual([wait['code'] for wait in row['waits']], ['paused'])
        self.assertEqual(row['power'], 'on')
        text = self.box.wb_env('hub', 'list', cwd=self.plain).stdout
        self.assertIn('일시정지', [line for line in text.splitlines() if 'alpha' in line][0])
        outcome = self.box.drive('resume', self.entry)
        self.assertTrue(outcome['ok'], outcome)
        self.assertIs(self.box.status(self.alpha)['automation']['paused'], False)
        self.assertEqual(self.row()['waits'], [])
        self.assertTrue(alive(sleeper), 'pause/resume must not touch the user job')

    def test_an_alive_but_silent_backend_is_unknown_never_off(self):
        stop = start_ticks(self.pid)
        self.assertEqual(stop, self.ticks)
        self.addCleanup(kill_exact, self.pid, self.ticks, signal.SIGCONT)
        self.assertTrue(kill_exact(self.pid, self.ticks, signal.SIGSTOP))
        time.sleep(0.5)
        row = self.row()
        self.assertEqual(row['power'], 'unknown', row)
        self.assertNotEqual(row['power'], 'off')
        self.assertIn(row['power_detail'], ('timeout', 'unreachable'), row)
        self.assertEqual(row['waits'], [])
        table = self.box.wb_env('hub', 'list', cwd=self.plain).stdout
        self.assertIn('연결 없음/불명', [line for line in table.splitlines() if 'alpha' in line][0])
        self.assertTrue(kill_exact(self.pid, self.ticks, signal.SIGCONT))
        self.assertTrue(wait_until(lambda: self.row()['power'] == 'on', 60))


# ---------------------------------------------------------------------------------------------------------
class NonAttachedControlTests(Case):
    """HB-T02 on a real backend: only the Hub's client name may pause/resume without attaching."""

    def setUp(self):
        super().setUp()
        self.alpha, self.alpha_id, self.entry = self.register_project('alpha')
        self.box.power_on(self.alpha, self.alpha_id)

    def test_other_client_names_are_refused_the_hub_name_is_accepted_and_the_attach_slot_is_unchanged(self):
        from workbench.backend.client import UiClient
        from workbench.contracts.ui_v1 import ClientType
        pid, ticks, record = self.box.backend_ref(self.alpha)
        expected = {'pid': pid, 'start_ticks': ticks}
        client = self.box.attach(self.alpha)
        for name in ('workbench-hub-x', 'someone-else', 'workbench-ui'):
            with self.subTest(client=name):
                other = UiClient(Path(record['ui_socket']), name=name, timeout=10, expected_process=expected)
                self.addCleanup(other.close)
                for kind, extra in ((ClientType.PAUSE, {}), (ClientType.RESUME, {'reconciled': True})):
                    reply = other.request(kind, **extra)
                    self.assertFalse(reply.get('ok'), reply)
                    self.assertEqual(reply.get('reason'), 'not_attached', reply)
        self.assertIs(self.box.status(self.alpha)['automation']['paused'], False)
        outcome = self.box.drive('pause', self.entry)  # the Hub's own client name
        self.assertTrue(outcome['ok'], outcome)
        snapshot = self.box.status(self.alpha)
        self.assertIs(snapshot['automation']['paused'], True)
        self.assertIs(snapshot['attached'], True, 'the control must not take or free the attach slot')
        self.assertTrue(wait_until(lambda: client.pump(0.1) and any(
            (state.get('automation') or {}).get('paused') is True for state in client.states), 10),
            'the attached UI is pushed the new state')
        self.assertTrue(self.box.drive('resume', self.entry)['ok'])
        self.assertIs(self.box.status(self.alpha)['automation']['paused'], False)


# ---------------------------------------------------------------------------------------------------------
class UpdateInteractionTests(Case):
    """HB-AC-18 (live part): `wb update` outside an installed tool refuses before stopping the Hub or any backend.

    The full update flow (Hub stopped before the first backend, failure ordering, fence held against a Hub
    start) is fixture evidence in ``tests/backend/maintenance/test_update_hub*.py`` with the update fence
    relocated; a real reinstall with a new version is ``pending`` on the target host."""
    autostart = '1'

    def test_a_refused_update_leaves_the_hub_and_the_running_backend_untouched(self):
        alpha, alpha_id, entry = self.register_project('alpha')
        self.box.register(entry)
        pid, ticks = self.box.power_on(alpha, alpha_id)
        hub_pid, hub_ticks = self.box.start_hub()
        fence = self.box.base / 'fence'  # the update fence is relocated: the user's own fence is never taken
        done = subprocess.run([sys.executable, '-c', UPDATE_DRIVER], cwd=self.plain, stdin=subprocess.DEVNULL,
                              env=self.box.env_with(WB_TEST_FENCE_BASE=str(fence), PYTHONPATH=str(REPO / 'src')),
                              capture_output=True, text=True, timeout=120)
        self.assertNotEqual(done.returncode, 0, 'a dev checkout is not an installed tool: the update must refuse')
        self.assertTrue(alive(hub_pid, hub_ticks), done.stdout + done.stderr)
        self.assertTrue(alive(pid, ticks))
        self.assertEqual(self.box.hub_lock_holder(), hub_pid)
        self.assertEqual(self.box.hub_row(alpha)['power'], 'on')


if __name__ == '__main__':
    unittest.main()
