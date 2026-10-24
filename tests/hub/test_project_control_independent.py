"""Independent verification of ``workbench.hub.projects`` (HB-07, seam S6): races, lost connections,
retry bounds, attach-slot discipline, mismatch refusal without contact and the unchanged ``wb shutdown``.

Expected values come from the SPEC "프로젝트 제어 라이브러리" / "상태 전이", the ticket HB-07 acceptance
criteria (HB-AC-02/03/08/09/13) and BRIEF C-AC-22 (a missing or unverified shutdown result is never a
success), not from the implementation. Real backends run with the fake OMP
(``tests/recovery_boot/fake_omp.py``: no model, no provider, no network); backends the contract
needs to misbehave are the contract fixture backend (``fixtures/contract_backend.py``). Every library
call runs in a driver subprocess the test starts.

Hygiene is the worker suite's ``ProjectSandbox`` (temp HOME/XDG, short private ``XDG_RUNTIME_DIR``,
placeholder ``agent.db``, ``WORKBENCH_HUB_AUTOSTART=0``, exact-identity process cleanup). A real
backend prepares its OMP namespace under ``/tmp/omp-workbench-<uid>/<hash>`` by design: the sandbox
checks that exact path absent when the project is made and removes only that directory after every
process of the sandbox has ended. Run with ``discover -s tests/hub -p 'test_project_control_independent.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_control import (  # noqa: E402
    CONTRACT_BACKEND, ProjectSandbox, alive, kill_exact, parent_and_session, start_ticks, wait_until)

VERIFIED = {'verified': True, 'problems': []}
CLOSING = {'v': 1, 'type': 'closing', 'reason': 'backend_shutdown'}
TOKEN_MISMATCH = {'ok': False, 'reason': 'shutdown_token_mismatch', 'detail': 'work changed'}
CHANGED = '작업이 바뀌었습니다'


class Case(unittest.TestCase):
    def setUp(self):
        self.box = ProjectSandbox(self)

    # -- fixture backends --------------------------------------------------------------------
    def fixture(self, name: str, **options) -> tuple[Path, str, Path]:
        """A contract fixture backend on a fresh project; ``(root, project_id, frames_log)``."""
        root, project_id = self.box.project(name)
        log = self.box.base / f'frames-{name}.jsonl'
        argv = [sys.executable, str(CONTRACT_BACKEND), str(root), '--frames-log', str(log)]
        for key, value in options.items():
            argv += ['--' + key.replace('_', '-'), value if isinstance(value, str) else json.dumps(value)]
        process = subprocess.Popen(argv, env=self.box.env, cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
        self.box.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        return root, project_id, log

    @staticmethod
    def frames(log: Path) -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def sent(self, log: Path, kind: str) -> list[dict]:
        return [item['header'] for item in self.frames(log) if item['header'].get('type') == kind]

    def own_process(self, seconds: int = 120) -> dict:
        """A live process of this test: the identity a fixture backend can claim as its own."""
        process = subprocess.Popen(['sleep', str(seconds)], cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.box.processes.append(process)
        return {'role': 'backend', 'pid': process.pid, 'start_ticks': start_ticks(process.pid), 'owner_epoch': 1}

    def ended_process(self) -> dict:
        process = subprocess.Popen(['sleep', '30'], cwd=self.box.base, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        ref = {'role': 'backend', 'pid': process.pid, 'start_ticks': start_ticks(process.pid), 'owner_epoch': 1}
        process.terminate()
        process.wait(timeout=10)
        return ref

    @staticmethod
    def pending(ref: dict | None = None, active: list | None = None) -> dict:
        reply = {'token': 't-1', 'active': active or [], 'expires_in': 120}
        if ref is not None:
            reply['processes'] = {'backend': ref}
        return {'shutdown_request': reply}


class ConcurrentPowerOnTests(Case):
    """Two (three) callers racing to power the same project on make exactly one backend."""

    def test_three_simultaneous_power_ons_of_one_project_leave_exactly_one_backend(self):
        root, project_id = self.box.project('alpha')
        entry = self.box.entry(root, project_id)
        runs = [self.box.drive_async('power_on', entry) for _ in range(3)]
        for driver, _result in runs:
            self.assertEqual(driver.wait(timeout=240), 0)
        outcomes = [json.loads(result.read_text()) for _driver, result in runs]
        self.assertTrue(all(item['ok'] for item in outcomes), outcomes)
        pid, ticks, _record = self.box.backend_ref(root)
        self.assertEqual({json.dumps(item['data']['backend'], sort_keys=True) for item in outcomes},
                         {json.dumps({'pid': pid, 'start_ticks': ticks}, sort_keys=True)})
        self.assertEqual(self.box.backends(), {pid: ticks})
        self.assertEqual(self.box.lock_holders(root), {pid})
        panes = self.box.pane_processes(self.box.status(root))
        self.assertEqual(set(panes), {'manager_omp', 'worker_omp', 'host_shell'})
        self.assertTrue(all(alive(*ref) for ref in panes.values()), panes)
        self.assertEqual(len(os.listdir(self.box.run)), 1, os.listdir(self.box.run))  # one runtime root


class HubEndingDuringPowerOnTests(Case):
    """The backend a power-on starts depends neither on the Hub nor on whoever called (HB-AC-02/03)."""

    def hub_ending_during_power_on(self, end) -> None:
        root, project_id = self.box.project('alpha')
        entry = self.box.entry(root, project_id)
        self.box.register(entry)
        hub_pid, hub_ticks = self.box.start_hub()
        driver, result = self.box.drive_async('power_on', entry)
        time.sleep(0.4)
        self.assertIsNone(driver.poll(), 'power_on already ended: the Hub would not be ended during it')
        end(hub_pid, hub_ticks)
        self.assertTrue(wait_until(lambda: not alive(hub_pid, hub_ticks), 20))
        self.assertEqual(driver.wait(timeout=240), 0)
        outcome = json.loads(result.read_text())
        self.assertTrue(outcome['ok'], outcome)
        pid, ticks, _record = self.box.backend_ref(root)
        self.assertEqual(outcome['data']['backend'], {'pid': pid, 'start_ticks': ticks})
        time.sleep(1.5)  # anything tied to the Hub or the driver would end now
        self.assertTrue(alive(pid, ticks))
        self.assertNotIn(parent_and_session(pid)[0], (hub_pid, driver.pid))
        panes = self.box.pane_processes(self.box.status(root))
        self.assertEqual(set(panes), {'manager_omp', 'worker_omp', 'host_shell'})
        self.assertTrue(all(alive(*ref) for ref in panes.values()), panes)
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['phase'], snapshot['backend']['pid']), ('ready', pid))
        self.box.start_hub()
        row = self.box.hub_row(root)
        self.assertEqual((row['power'], row['backend']), ('on', {'pid': pid, 'start_ticks': ticks}), row)

    def test_wb_hub_stop_during_power_on(self):
        def end(_pid, _ticks):
            done = self.box.wb('hub', 'stop', timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.hub_ending_during_power_on(end)

    def test_hub_sigkill_during_power_on(self):
        self.hub_ending_during_power_on(lambda pid, ticks: self.assertTrue(kill_exact(pid, ticks)))


class ShutdownTokenRaceTests(Case):
    """An attached UI also asks to shut down while ``power_off`` waits for its answer."""

    def running(self):
        root, project_id = self.box.project('alpha')
        pid, ticks = self.box.power_on(root, project_id)
        self.box.attach_client = self.box.attach(root)
        panes = self.box.pane_processes(self.box.status(root))
        return root, project_id, pid, ticks, panes

    def start_power_off(self, root, project_id, answers):
        paths = {name: self.box.base / name for name in ('asks.jsonl', 'gate', 'out.txt')}
        driver, result = self.box.drive_async('power_off', self.box.entry(root, project_id), answers=answers,
                                              asks=str(paths['asks.jsonl']), gate=str(paths['gate']),
                                              out=str(paths['out.txt']))
        return driver, result, paths

    def count_asks(self, paths):
        return len(self.box.asks(paths['asks.jsonl']))

    def await_closing(self, client):
        self.assertTrue(wait_until(lambda: client.closing is not None or not client.pump(0.2), 60))
        return client.closing

    def test_a_ui_shutdown_request_while_power_off_waits_makes_it_ask_again_and_shuts_down_once(self):
        root, project_id, pid, ticks, panes = self.running()
        ui = self.box.attach_client
        driver, result, paths = self.start_power_off(root, project_id, [True, True])
        self.assertTrue(wait_until(lambda: self.count_asks(paths) == 1, 60))
        from workbench.contracts.ui_v1 import ClientType
        taken = ui.request(ClientType.SHUTDOWN_REQUEST)  # the backend keeps one token: the driver's is replaced
        self.assertTrue(taken.get('token'), taken)
        Path(f"{paths['gate']}.1").touch()
        self.assertTrue(wait_until(lambda: self.count_asks(paths) == 2, 60))
        self.assertTrue(alive(pid, ticks))  # the mismatched confirm stopped nothing
        Path(f"{paths['gate']}.2").touch()
        self.assertEqual(driver.wait(timeout=90), 0)
        outcome = json.loads(result.read_text())
        self.assertTrue(outcome['ok'], outcome)
        self.assertIs(outcome['data']['result']['verified'], True)
        self.assertEqual(self.count_asks(paths), 2)
        self.assertEqual(paths['out.txt'].read_text().count(CHANGED), 1)
        closing = self.await_closing(ui)
        self.assertIs(closing['result']['verified'], True)  # the UI saw the one result as well
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 20))
        self.assertTrue(wait_until(lambda: not any(alive(*ref) for ref in panes.values()), 20), panes)
        self.assertEqual(self.box.backends(), {})

    def test_a_ui_that_also_confirms_its_own_shutdown_never_makes_a_false_success_or_a_second_shutdown(self):
        root, project_id, pid, ticks, panes = self.running()
        ui = self.box.attach_client
        driver, result, paths = self.start_power_off(root, project_id, [True, True])
        self.assertTrue(wait_until(lambda: self.count_asks(paths) == 1, 60))
        from workbench.contracts.ui_v1 import ClientType
        pending = ui.request(ClientType.SHUTDOWN_REQUEST)
        confirmed = ui.request(ClientType.SHUTDOWN_CONFIRM, token=pending['token'])
        self.assertTrue(confirmed.get('ok'), confirmed)
        Path(f"{paths['gate']}.1").touch()
        Path(f"{paths['gate']}.2").touch()
        self.assertEqual(driver.wait(timeout=120), 0, 'power_off must end, not hang, when the UI shut it down')
        outcome = json.loads(result.read_text())
        # the library reports once; whatever it reports, it is never a success without a verified result
        if outcome['ok']:
            self.assertIs(outcome['data']['result']['verified'], True, outcome)
        else:
            self.assertIn(outcome['reason'], {'unreachable', 'no_result', 'not_running', 'unverified',
                                              'shutdown_token_mismatch', 'shutdown_not_requested',
                                              'shutting_down', 'declined'}, outcome)
        self.assertLessEqual(self.count_asks(paths), 2)
        closing = self.await_closing(ui)  # the UI's own shutdown completed exactly once and verified
        self.assertIs(closing['result']['verified'], True)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 20))
        self.assertTrue(wait_until(lambda: not any(alive(*ref) for ref in panes.values()), 20), panes)
        self.assertEqual(self.box.backends(), {})

    def test_work_that_ends_before_the_confirm_is_asked_again_with_the_shorter_list(self):
        root, project_id, pid, ticks, panes = self.running()
        sleeper = self.box.sleeper(root, 311)
        driver, result, paths = self.start_power_off(root, project_id, [True, True])
        self.assertTrue(wait_until(lambda: self.count_asks(paths) == 1, 60))
        first = self.box.asks(paths['asks.jsonl'])[0]['active']
        self.assertIn(sleeper, [proc.get('pid') for item in first for proc in item.get('processes') or ()], first)
        self.assertTrue(kill_exact(sleeper, start_ticks(sleeper)))  # the user's work ends after the list was shown
        self.assertTrue(wait_until(lambda: not alive(sleeper), 10))
        Path(f"{paths['gate']}.1").touch()
        self.assertTrue(wait_until(lambda: self.count_asks(paths) == 2, 60))
        second = self.box.asks(paths['asks.jsonl'])[1]['active']
        self.assertNotIn(sleeper, [proc.get('pid') for item in second for proc in item.get('processes') or ()],
                         second)
        self.assertNotEqual(first, second)
        Path(f"{paths['gate']}.2").touch()
        self.assertEqual(driver.wait(timeout=90), 0)
        outcome = json.loads(result.read_text())
        self.assertTrue(outcome['ok'], outcome)
        self.assertIs(outcome['data']['result']['verified'], True)
        self.assertEqual(self.count_asks(paths), 2)
        self.assertTrue(wait_until(lambda: not alive(pid, ticks), 20))


class ResultLossTests(Case):
    """Connection lost or no result after the confirm: ``no_result`` with the backend's exact state."""

    CONFIRM = {'shutting_down': True, 'result_deadline': 5}

    def power_off(self, root, project_id, **fields):
        return self.box.drive('power_off', self.box.entry(root, project_id), answers=[True], **fields)

    def test_connection_lost_while_waiting_for_the_result_names_an_ended_backend(self):
        ref = self.ended_process()
        root, project_id, log = self.fixture('alpha', replies_json={**self.pending(ref),
            'shutdown_confirm': self.CONFIRM}, close_after='shutdown_confirm', closing_json=None)
        outcome = self.power_off(root, project_id)
        self.assertEqual((outcome['ok'], outcome['reason'], outcome['data']['backend']),
                         (False, 'no_result', 'ended'), outcome)
        self.assertIn('closed the connection', outcome['detail'])
        self.assertEqual(len(self.sent(log, 'shutdown_confirm')), 1)

    def test_connection_lost_while_waiting_for_the_result_names_a_still_running_backend(self):
        ref = self.own_process()
        root, project_id, _log = self.fixture('alpha', replies_json={**self.pending(ref),
            'shutdown_confirm': self.CONFIRM}, close_after='shutdown_confirm', closing_json=None)
        started = time.monotonic()
        outcome = self.power_off(root, project_id)
        self.assertEqual((outcome['ok'], outcome['reason'], outcome['data']['backend']),
                         (False, 'no_result', 'alive'), outcome)
        self.assertGreaterEqual(time.monotonic() - started, 5)  # it watched the exact pid before saying alive
        self.assertTrue(alive(ref['pid'], ref['start_ticks']))  # and signalled nobody

    def test_a_connection_lost_on_the_confirm_itself_is_no_result_not_success(self):
        ref = self.ended_process()
        root, project_id, log = self.fixture('alpha', replies_json=self.pending(ref),
                                             close_after='shutdown_request', closing_json=None)
        outcome = self.power_off(root, project_id)
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'no_result'), outcome)
        self.assertEqual(outcome['data']['backend'], 'ended')

    def test_a_closing_frame_without_a_result_is_no_result(self):
        for name, closing in (('absent', CLOSING), ('null', {**CLOSING, 'result': None}),
                              ('not_a_mapping', {**CLOSING, 'result': 'verified'})):
            with self.subTest(result=name):
                root, project_id, _log = self.fixture(name, replies_json={**self.pending(),
                    'shutdown_confirm': self.CONFIRM}, close_after='shutdown_confirm', closing_json=closing)
                outcome = self.power_off(root, project_id)
                self.assertEqual((outcome['ok'], outcome['reason']), (False, 'no_result'), outcome)
                self.assertNotIn('result', outcome['data'])

    def test_an_empty_or_falsy_verified_result_is_unverified_never_success(self):
        for name, result in (('empty', {}), ('false', {'verified': False}), ('none', {'verified': None}),
                             ('zero', {'verified': 0, 'problems': ['x']})):
            with self.subTest(result=name):
                root, project_id, _log = self.fixture(name, replies_json={**self.pending(),
                    'shutdown_confirm': self.CONFIRM}, close_after='shutdown_confirm',
                    closing_json={**CLOSING, 'result': result})
                outcome = self.power_off(root, project_id)
                self.assertEqual((outcome['ok'], outcome['reason']), (False, 'unverified'), outcome)

    def test_a_verified_result_with_recorded_problems_is_still_returned_as_the_backends_result(self):
        root, project_id, _log = self.fixture('alpha', replies_json={**self.pending(),
            'shutdown_confirm': self.CONFIRM}, close_after='shutdown_confirm',
            closing_json={**CLOSING, 'result': VERIFIED})
        outcome = self.power_off(root, project_id)
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(outcome['data']['result'], VERIFIED)


class ReAskBoundTests(Case):
    """``shutdown_token_mismatch`` re-asks with a new request at most five times; no second confirm without it."""

    def test_a_mismatch_on_every_confirm_stops_after_one_ask_and_five_new_ones(self):
        root, project_id, log = self.fixture('alpha', replies_json={**self.pending(), 'shutdown_confirm': TOKEN_MISMATCH})
        out = self.box.base / 'out.txt'
        asks = self.box.base / 'asks.jsonl'
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[True] * 12,
                                 asks=str(asks), out=str(out))
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'work_kept_changing'), outcome)
        self.assertEqual(len(self.box.asks(asks)), 6)
        self.assertEqual(len(self.sent(log, 'shutdown_request')), 6)
        self.assertEqual(len(self.sent(log, 'shutdown_confirm')), 6)
        text = out.read_text()
        self.assertGreaterEqual(text.count(CHANGED), 5)
        self.assertIn('작업이 계속 바뀌어 끄지 않았습니다', text)
        self.assertEqual(self.box.backends(), {})  # a fixture holds no real backend; nothing else was started

    def test_a_decline_sends_no_confirm_at_all(self):
        root, project_id, log = self.fixture('alpha', replies_json=self.pending(active=[{'kind': 'task_run'}]))
        asks = self.box.base / 'asks.jsonl'
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[False], asks=str(asks))
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'declined'), outcome)
        self.assertEqual(self.box.asks(asks), [{'call': 1, 'active': [{'kind': 'task_run'}]}])
        self.assertEqual(self.sent(log, 'shutdown_confirm'), [])
        self.assertEqual(len(self.sent(log, 'shutdown_request')), 1)

    def test_a_mismatch_then_a_decline_stops_nothing_and_confirms_once(self):
        root, project_id, log = self.fixture('alpha', replies_json={**self.pending(), 'shutdown_confirm': TOKEN_MISMATCH})
        asks = self.box.base / 'asks.jsonl'
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[True, False],
                                 asks=str(asks))
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'declined'), outcome)
        self.assertEqual(len(self.box.asks(asks)), 2)
        self.assertEqual(len(self.sent(log, 'shutdown_confirm')), 1)

    def test_any_other_refusal_is_returned_as_is_and_not_asked_again(self):
        refusal = {'ok': False, 'reason': 'shutdown_not_requested', 'detail': 'request shutdown first'}
        root, project_id, log = self.fixture('alpha', replies_json={**self.pending(), 'shutdown_confirm': refusal})
        asks = self.box.base / 'asks.jsonl'
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[True, True], asks=str(asks))
        self.assertEqual((outcome['ok'], outcome['reason'], outcome['detail']),
                         (False, 'shutdown_not_requested', 'request shutdown first'), outcome)
        self.assertEqual(len(self.box.asks(asks)), 1)
        self.assertEqual(len(self.sent(log, 'shutdown_confirm')), 1)

    def test_a_refused_request_is_returned_and_never_asked_about(self):
        refusal = {'shutdown_request': {'ok': False, 'reason': 'invalid_message', 'detail': 'no'}}
        root, project_id, log = self.fixture('alpha', replies_json=refusal)
        asks = self.box.base / 'asks.jsonl'
        outcome = self.box.drive('power_off', self.box.entry(root, project_id), answers=[True], asks=str(asks))
        self.assertEqual((outcome['ok'], outcome['reason']), (False, 'invalid_message'), outcome)
        self.assertFalse(asks.exists())
        self.assertEqual(self.sent(log, 'shutdown_confirm'), [])


class AttachSlotTests(Case):
    """``pause``/``resume``/``confirm_boot`` never attach: the slot, the input and the screen stay as they were."""

    def test_the_library_sends_only_a_workbench_hub_hello_and_never_an_attach(self):
        snapshot = {'boot': {'boot_id': 'boot-xyz', 'confirmation_required': True}}
        root, project_id, log = self.fixture('alpha', snapshot_json=snapshot)
        entry = self.box.entry(root, project_id)
        for op in ('pause', 'resume', 'confirm_boot'):
            self.assertTrue(self.box.drive(op, entry)['ok'], op)
        kinds = [item['header']['type'] for item in self.frames(log)]
        self.assertNotIn('attach', kinds)
        self.assertNotIn('input', kinds)
        self.assertEqual({item['header'].get('client') for item in self.frames(log)
                          if item['header']['type'] == 'hello'}, {'workbench-hub'})
        self.assertEqual(self.sent(log, 'resume')[0].get('reconciled'), True)
        self.assertEqual(len(self.sent(log, 'pause')), 1)
        confirm = self.sent(log, 'confirm_boot')
        self.assertEqual([item.get('boot_id') for item in confirm], ['boot-xyz'])

    def test_confirm_boot_passes_each_backend_refusal_through_unchanged(self):
        for reason in ('boot_confirmation_not_required', 'boot_id_mismatch', 'boot_confirmation_not_saved'):
            with self.subTest(reason=reason):
                root, project_id, _log = self.fixture(reason, snapshot_json={'boot': {'boot_id': 'b1'}},
                    replies_json={'confirm_boot': {'ok': False, 'reason': reason, 'detail': f'{reason} detail'}})
                outcome = self.box.drive('confirm_boot', self.box.entry(root, project_id))
                self.assertEqual((outcome['ok'], outcome['reason'], outcome['detail']),
                                 (False, reason, f'{reason} detail'), outcome)

    def test_confirm_boot_without_a_current_boot_id_sends_no_confirm(self):
        for number, boot in enumerate((None, {}, {'boot_id': ''}, {'boot_id': 7})):
            with self.subTest(boot=boot):
                root, project_id, log = self.fixture(f'boot{number}', snapshot_json={'boot': boot})
                outcome = self.box.drive('confirm_boot', self.box.entry(root, project_id))
                self.assertEqual((outcome['ok'], outcome['reason']), (False, 'boot_id_unavailable'), outcome)
                self.assertEqual(self.sent(log, 'confirm_boot'), [])

    def test_pause_with_no_screen_leaves_the_attach_slot_free_and_resume_with_one_leaves_it_alone(self):
        root, project_id = self.box.project('alpha')
        self.box.power_on(root, project_id)
        entry = self.box.entry(root, project_id)
        self.assertTrue(self.box.drive('pause', entry)['ok'])
        snapshot = self.box.status(root)
        self.assertIs(snapshot['automation']['paused'], True)
        self.assertIs(snapshot['attached'], False)  # the request did not take the slot
        client = self.box.attach(root)  # so a user can attach afterwards, and sees the pause
        self.assertIs(self.box.status(root)['attached'], True)
        self.assertTrue(self.box.drive('resume', entry)['ok'])
        snapshot = self.box.status(root)
        self.assertEqual((snapshot['automation']['paused'], snapshot['attached']), (False, True))
        from workbench.contracts.ui_v1 import ClientType
        self.assertTrue(client.request(ClientType.FOCUS, pane='manager_omp').get('ok'))

    def test_an_attached_ui_keeps_typing_while_the_library_pauses_and_resumes(self):
        root, project_id = self.box.project('alpha')
        self.box.power_on(root, project_id)
        self.box.attach_client = self.box.attach(root)
        client = self.box.attach_client
        entry = self.box.entry(root, project_id)
        marker = f'hb07-{uuid.uuid4().hex[:8]}'
        for op in ('pause', 'resume', 'pause'):
            self.assertTrue(self.box.drive(op, entry)['ok'], op)
            self.box.type_shell(client, f'echo {marker}\n')  # still the attached client; input is accepted
            self.assertIs(self.box.status(root)['attached'], True)
        self.assertIs(self.box.status(root)['automation']['paused'], True)
        client.pump(0.5)
        # the driver's connections are gone; the UI connection was never closed by the backend
        from workbench.contracts.ui_v1 import ClientType
        self.assertTrue(client.request(ClientType.FOCUS, pane='host_shell').get('ok'))

    def test_every_function_on_a_stopped_project_is_not_running_and_starts_nothing_but_power_on(self):
        root, project_id = self.box.project('alpha')
        entry = self.box.entry(root, project_id)
        for op in ('power_off', 'pause', 'resume', 'confirm_boot'):
            with self.subTest(op=op):
                asks = self.box.base / f'asks-{op}.jsonl'
                outcome = self.box.drive(op, entry, answers=[True], asks=str(asks))
                self.assertEqual((outcome['ok'], outcome['reason']), (False, 'not_running'), outcome)
                self.assertEqual(self.box.ours(), {})
                self.assertFalse(asks.exists())
                self.assertEqual(os.listdir(self.box.run), [])


class MismatchWithoutContactTests(Case):
    """A mismatched entry is refused before any backend is contacted, even when one is listening."""

    def test_no_function_contacts_a_live_backend_for_a_mismatched_entry(self):
        root, project_id, log = self.fixture('alpha')
        other_id = str(uuid.uuid4())
        broken, broken_id, broken_log = self.fixture('broken')
        os.chmod(broken / '.wb' / 'project.json', 0o644)  # read_project_marker refuses it from now on
        cases = [(self.box.entry(root, other_id), 'project_id_mismatch', log),
                 (self.box.entry(broken, broken_id), 'marker_invalid', broken_log)]
        asks = self.box.base / 'asks.jsonl'
        before = self.box.ours()
        for op in ('power_on', 'power_off', 'pause', 'resume', 'confirm_boot'):
            for entry, detail, frames_log in cases:
                with self.subTest(op=op, detail=detail):
                    outcome = self.box.drive(op, entry, answers=[True, True], asks=str(asks))
                    self.assertEqual((outcome['ok'], outcome['reason'], outcome['detail']),
                                     (False, 'mismatch', detail), outcome)
                    self.assertEqual(self.frames(frames_log), [], 'a backend was contacted')
                    self.assertFalse(asks.exists())
                    self.assertEqual(self.box.ours(), before)  # no process was started, none ended
        self.assertEqual(self.box.backends(), {})

    def test_a_missing_root_and_an_uninitialised_folder_are_refused_by_every_function(self):
        bare = self.box.folder('bare')
        gone = self.box.base / 'work' / 'gone'
        cases = [(self.box.entry(gone, str(uuid.uuid4())), 'root_missing'),
                 (self.box.entry(bare, str(uuid.uuid4())), 'not_initialized')]
        for op in ('power_on', 'power_off', 'pause', 'resume', 'confirm_boot'):
            for entry, detail in cases:
                with self.subTest(op=op, detail=detail):
                    outcome = self.box.drive(op, entry, answers=[True])
                    self.assertEqual((outcome['ok'], outcome['reason'], outcome['detail']),
                                     (False, 'mismatch', detail), outcome)
                    self.assertFalse(os.path.lexists(bare / '.wb'))
                    self.assertFalse(gone.exists())
                    self.assertEqual(self.box.ours(), {})


class ShutdownCliUnchangedTests(Case):
    """``wb shutdown`` prints exactly what main's did (outputs captured from main 3c36901 for the same
    fixture backend; the confirm-and-wait loop moved into ``cli.confirm_shutdown``)."""

    CONFIRM = {'shutting_down': True, 'result_deadline': 5}

    def run_shutdown(self, name, *flags, **options):
        root, _project_id, log = self.fixture(name, **options)
        done = self.box.wb('shutdown', *flags, cwd=root, timeout=120)
        return done.returncode, done.stdout, done.stderr, log

    def scenarios(self) -> dict:
        active = [{'kind': 'task_run', 'task_id': 'T1'}]
        verified = {**CLOSING, 'result': VERIFIED}
        unverified = {**CLOSING, 'result': {'verified': False, 'problems': ['host_shell survived']}}
        ended = self.ended_process()
        return {
            'verified': (('--yes',), dict(replies_json={**self.pending(active=active), 'shutdown_confirm': self.CONFIRM},
                                          close_after='shutdown_confirm', closing_json=verified)),
            'verified_json': (('--yes', '--json'), dict(replies_json={**self.pending(), 'shutdown_confirm': self.CONFIRM},
                                                       close_after='shutdown_confirm', closing_json=verified)),
            'unverified': (('--yes',), dict(replies_json={**self.pending(active=active), 'shutdown_confirm': self.CONFIRM},
                                            close_after='shutdown_confirm', closing_json=unverified)),
            'refused': (('--yes',), dict(replies_json={**self.pending(), 'shutdown_confirm': TOKEN_MISMATCH})),
            'lost': (('--yes',), dict(replies_json={**self.pending(ended), 'shutdown_confirm': self.CONFIRM},
                                      close_after='shutdown_confirm', closing_json=None)),
            'lost_json': (('--yes', '--json'), dict(replies_json={**self.pending(ended), 'shutdown_confirm': self.CONFIRM},
                                                   close_after='shutdown_confirm', closing_json=None)),
            'unconfirmed': ((), dict(replies_json=self.pending(active=active))),
        }

    EXPECTED = {
        'verified': (0,
                     'active work:\n  - {"kind": "task_run", "task_id": "T1"}\n'
                     'shutdown result: {"problems": [], "verified": true}\n', ''),
        'verified_json': (0, 'no active work reported\n{"shutdown": {"problems": [], "verified": true}}\n', ''),
        'unverified': (1,
                       'active work:\n  - {"kind": "task_run", "task_id": "T1"}\n'
                       'shutdown result: {"problems": ["host_shell survived"], "verified": false}\n',
                       '종료 확인 실패: 일부 process의 종료를 확인하지 못했습니다 (host_shell survived)\n'),
        'refused': (1, 'no active work reported\n',
                    'shutdown refused: shutdown_token_mismatch: work changed\n'),
        'lost': (1, 'no active work reported\nshutdown result: null\n',
                 '종료 중: 진행 중인 OMP turn 중단과 process 정리를 기다립니다 (최대 5초)\n'
                 '종료 확인 실패: 종료 결과를 받지 못해 OMP·host shell 등의 종료를 확인할 수 없습니다 '
                 "(backend process는 종료됨; the backend closed the connection before its result). 'status'로 확인하세요\n"),
        'lost_json': (1,
                      'no active work reported\n'
                      '{"shutdown": null, "unconfirmed": {"backend": "ended", "cause": '
                      '"the backend closed the connection before its result"}}\n',
                      '종료 확인 실패: 종료 결과를 받지 못해 OMP·host shell 등의 종료를 확인할 수 없습니다 '
                      "(backend process는 종료됨; the backend closed the connection before its result). 'status'로 확인하세요\n"),
        'unconfirmed': (1, 'active work:\n  - {"kind": "task_run", "task_id": "T1"}\n',
                        'refusing to shut down without confirmation: confirm with --yes (nothing was stopped). '
                        '종료하려면 --yes를 붙여 다시 실행하세요\n'),
    }

    def test_wb_shutdown_output_and_exit_codes_match_main(self):
        for name, (flags, options) in self.scenarios().items():
            with self.subTest(scenario=name):
                code, out, err, log = self.run_shutdown(name, *flags, **options)
                expected = self.EXPECTED[name]
                self.assertEqual((code, out, err), expected)
                self.assertEqual(len(self.sent(log, 'shutdown_request')), 1)  # the CLI never re-asks
                confirms = len(self.sent(log, 'shutdown_confirm'))
                self.assertEqual(confirms, 0 if name == 'unconfirmed' else 1)


if __name__ == '__main__':
    unittest.main()
