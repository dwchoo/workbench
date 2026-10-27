"""``wb shutdown --all`` (2026-10-27 사용자 요청: 전체 끄기), seam: the ``wb`` CLI subprocess.

Every ``wb`` is a real subprocess in a temp HOME/XDG sandbox with a short private ``XDG_RUNTIME_DIR``
and ``WORKBENCH_HUB_AUTOSTART=0``. The "on" projects are ``fixtures/contract_backend.py`` processes
(no OMP, no real backend) whose ``shutdown_request``/``shutdown_confirm`` answers and ``closing``
result the test chooses; ``--frames-log`` shows which of them were sent a ``shutdown_confirm``.
Nothing here touches a real project or the real Hub. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from test_hub_cli_registry import CliWithBackendCase, run_in_pty

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

NONE_RUNNING = '켜진 WB 프로젝트가 없습니다.'
CLOSING_OK = {'v': 1, 'type': 'closing', 'reason': 'backend_shutdown', 'result': {'verified': True}}
CONFIRM_OK = {'shutting_down': True, 'result_deadline': 5}


class ShutdownAllCase(CliWithBackendCase):
    def on_project(self, name: str, *, active=(), confirm=None, closing=CLOSING_OK, request=None) -> Path:
        """A registered project whose fixture backend answers a full shutdown."""
        root = self.project(name)
        entries = self.registered() if self.registry_path.exists() else []
        self.seed_registry([*entries, self.entry(root, self.marker_id(root))])
        self.frames = getattr(self, 'frames', {})
        self.frames[name] = self.base / f'{name}.frames'
        replies = {'shutdown_request': request or {'token': f't-{name}', 'active': list(active), 'expires_in': 120},
                   'shutdown_confirm': confirm or CONFIRM_OK}
        self.run_contract_backend(root, '--replies-json', json.dumps(replies), '--close-after', 'shutdown_confirm',
                                  '--closing-json', json.dumps(closing), '--frames-log', str(self.frames[name]))
        return root

    def off_project(self, name: str) -> Path:
        root = self.project(name)
        entries = self.registered() if self.registry_path.exists() else []
        self.seed_registry([*entries, self.entry(root, self.marker_id(root))])
        return root

    def sent(self, name: str) -> list[str]:
        path = self.frames[name]
        if not path.exists():
            return []
        return [json.loads(line)['header']['type'] for line in path.read_text().splitlines()]

    def shutdown_all(self, *args: str):
        return self.wb('shutdown', '--all', *args)


class NoProjectRunning(ShutdownAllCase):
    def test_nothing_on_says_so_and_succeeds(self):
        self.off_project('alpha')
        result = self.shutdown_all()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(NONE_RUNNING, result.stdout)

    def test_an_empty_registry_says_so_and_succeeds(self):
        result = self.shutdown_all()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(NONE_RUNNING, result.stdout)


class TwoRunningWithYes(ShutdownAllCase):
    def test_both_are_stopped_through_the_confirmed_shutdown_and_listed(self):
        self.on_project('alpha', active=[{'kind': 'task_run', 'task_id': 'T-alpha', 'run_id': 'r1'}])
        self.on_project('beta')
        self.off_project('gamma')
        result = self.shutdown_all('--yes')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for name in ('alpha', 'beta'):  # the same request -> confirm steps as ``wb shutdown``
            self.assertEqual(self.sent(name).count('shutdown_request'), 1, name)
            self.assertEqual(self.sent(name).count('shutdown_confirm'), 1, name)
        self.assertIn('T-alpha', result.stdout)  # the in-progress work is shown
        self.assertRegex(result.stdout, r'"alpha".*껐습니다')
        self.assertRegex(result.stdout, r'"beta".*껐습니다')
        self.assertNotIn('gamma', result.stdout)  # a project that was already off is not part of this
        self.assertNotIn(NONE_RUNNING, result.stdout)

    def test_the_projects_are_stopped_one_after_the_other(self):
        for name in ('alpha', 'beta'):
            self.on_project(name)
        result = self.shutdown_all('--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(result.stdout.index('"alpha"'), result.stdout.index('"beta"'))


class SingleConfirmation(ShutdownAllCase):
    def in_pty(self, *args: str, answer: str | None):
        argv = [sys.executable, '-m', 'workbench', 'shutdown', '--all', *args]
        return run_in_pty(argv, env=self.env, cwd=self.base, answer=answer)

    def test_no_stops_nothing_and_asks_only_once_for_all_projects(self):
        self.on_project('alpha', active=[{'kind': 'task_run', 'task_id': 'T-alpha', 'run_id': 'r1'}])
        self.on_project('beta')
        code, output = self.in_pty(answer='n')
        self.assertEqual(output.count('[y/N]'), 1, output)
        self.assertIn('T-alpha', output)
        self.assertNotEqual(code, 0, output)
        for name in ('alpha', 'beta'):
            self.assertNotIn('shutdown_confirm', self.sent(name), name)

    def test_yes_answer_stops_every_project(self):
        self.on_project('alpha')
        self.on_project('beta')
        code, output = self.in_pty(answer='y')
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count('[y/N]'), 1, output)
        for name in ('alpha', 'beta'):
            self.assertEqual(self.sent(name).count('shutdown_confirm'), 1, name)


class OneFails(ShutdownAllCase):
    UNVERIFIED = {'v': 1, 'type': 'closing', 'reason': 'backend_shutdown',
                  'result': {'verified': False, 'problems': ['host_shell survived']}}

    def test_a_failed_project_is_reported_the_others_still_stop_and_the_exit_is_not_zero(self):
        self.on_project('alpha')
        self.on_project('broken', closing=self.UNVERIFIED)
        self.on_project('beta')
        result = self.shutdown_all('--yes')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'"broken": 끄지 못했습니다 \(unverified: host_shell survived\)')
        for name in ('alpha', 'broken', 'beta'):  # all three were confirmed, in order, whatever the middle gave
            self.assertEqual(self.sent(name).count('shutdown_confirm'), 1, name)
        self.assertRegex(result.stdout, r'"alpha".*껐습니다')
        self.assertRegex(result.stdout, r'"beta".*껐습니다')

    def test_a_refused_request_is_a_failure_and_does_not_hold_back_the_rest(self):
        self.on_project('refusing', request={'ok': False, 'reason': 'shutting_down', 'detail': 'busy'})
        self.on_project('beta')
        result = self.shutdown_all('--yes')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('"refusing": 끄지 못했습니다 (shutting_down: busy)', result.stdout)
        self.assertEqual(self.sent('beta').count('shutdown_confirm'), 1)
        self.assertRegex(result.stdout, r'"beta".*껐습니다')

    def test_a_refused_token_is_retried_once_with_the_same_list_and_then_reported(self):
        stale = {'ok': False, 'reason': 'shutdown_token_mismatch', 'detail': 'work/process identity changed; request shutdown again'}
        self.on_project('moving', confirm=stale)  # every request lists the same work: only the token is refused
        result = self.shutdown_all('--yes')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.sent('moving').count('shutdown_request'), 2)  # asked again once, then it stopped trying
        self.assertEqual(self.sent('moving').count('shutdown_confirm'), 2)
        self.assertIn('"moving": 끄지 못했습니다 (shutdown_token_mismatch', result.stdout)


class NoTerminal(ShutdownAllCase):
    def test_without_yes_and_without_a_terminal_it_refuses_with_exit_2_and_stops_nothing(self):
        self.on_project('alpha')
        self.on_project('beta')
        result = self.shutdown_all()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('--yes', result.stderr)
        for name in ('alpha', 'beta'):
            self.assertNotIn('shutdown_confirm', self.sent(name), name)

    def test_the_work_is_still_shown_before_the_refusal(self):
        self.on_project('alpha', active=[{'kind': 'task_run', 'task_id': 'T-alpha', 'run_id': 'r1'}])
        result = self.shutdown_all()
        self.assertEqual(result.returncode, 2)
        self.assertIn('T-alpha', result.stdout)


class UnknownAndOptions(ShutdownAllCase):
    def test_a_project_that_cannot_be_observed_is_reported_and_not_counted_as_stopped(self):
        self.on_project('alpha')
        root = self.project('silent')
        self.seed_registry([*self.registered(), self.entry(root, self.marker_id(root))])
        self.run_contract_backend(root, '--snapshot-delay', '30')  # answers hello, never the snapshot in time
        result = self.shutdown_all('--yes')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'"silent": 상태 불명\(unknown: .+\)이라 끄지 않았고')
        self.assertNotRegex(result.stdout, r'"silent".*껐습니다')
        self.assertRegex(result.stdout, r'"alpha".*껐습니다')  # the others are still stopped

    def test_only_unobservable_projects_is_not_the_none_running_message(self):
        root = self.project('silent')
        self.seed_registry([self.entry(root, self.marker_id(root))])
        self.run_contract_backend(root, '--snapshot-delay', '30')
        result = self.shutdown_all('--yes')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(NONE_RUNNING, result.stdout)

    def test_all_cannot_be_combined_with_a_data_dir(self):
        root = self.project('alpha')
        result = self.wb('shutdown', '--all', '--data-dir', str(root / '.wb'), '--yes')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('--data-dir', result.stderr)

    def test_json_prints_one_result_object_per_run(self):
        self.on_project('alpha')
        self.on_project('broken', closing=OneFails.UNVERIFIED)
        result = self.shutdown_all('--yes', '--json')
        self.assertEqual(result.returncode, 1, result.stderr)
        listed = {item['name']: item['status'] for item in json.loads(result.stdout)['projects']}
        self.assertEqual(listed, {'alpha': 'stopped', 'broken': 'failed'})

    def test_a_damaged_registry_stops_nothing(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.registry_path.write_bytes(b'not json')
        self.registry_path.chmod(0o600)
        result = self.shutdown_all('--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn('registry', result.stderr)


class LibraryRaces(unittest.TestCase):
    """What a sandbox backend cannot be told to do: the work list changing between request and confirm."""

    def setUp(self):
        from unittest import mock
        from workbench.hub import projects, registry
        self.projects = projects
        self.entry = registry.Entry('/nowhere/alpha', 'p-1', 'alpha', 0.0, 'add')
        self.mock = mock

    def outcome(self, **kwargs):
        return self.projects.Outcome(**kwargs)

    def test_a_list_that_changed_since_it_was_shown_is_not_stopped(self):
        projects, mock = self.projects, self.mock
        shown = {'token': 't1', 'active': [], 'backend': {'pid': 1, 'start_ticks': 2}}
        changed = {'token': 't2', 'active': [{'kind': 'task_run', 'task_id': 'new'}], 'backend': shown['backend']}
        with mock.patch.object(projects, 'power_off_request', side_effect=[self.outcome(ok=True, data=shown),
                                                                          self.outcome(ok=True, data=changed)]), \
             mock.patch.object(projects, 'power_off_confirm', return_value=self.outcome(
                 ok=False, reason=projects.SHUTDOWN_TOKEN_MISMATCH, detail='changed')) as confirm:
            items = projects.power_off_all([self.entry], lambda pending: True)
        self.assertEqual(confirm.call_count, 1)  # the changed list was never confirmed
        self.assertEqual((items[0].status, items[0].reason), (projects.FAILED, projects.WORK_CHANGED))
        self.assertIn('작업이 바뀌었습니다', projects.stop_item_line(items[0]))

    def test_a_project_that_went_off_after_the_listing_is_already_off(self):
        projects, mock = self.projects, self.mock
        with mock.patch.object(projects, 'power_off_request', return_value=self.outcome(
                ok=False, reason=projects.NOT_RUNNING, detail='gone')), \
             mock.patch('workbench.hub.status.observe_entry', return_value={'power': 'off'}):
            items = projects.power_off_all([self.entry], lambda pending: True)
        self.assertEqual(items[0].status, projects.ALREADY_OFF)

    def test_not_running_without_a_fresh_off_is_not_called_off(self):
        projects, mock = self.projects, self.mock
        with mock.patch.object(projects, 'power_off_request', return_value=self.outcome(
                ok=False, reason=projects.NOT_RUNNING, detail='gone')), \
             mock.patch('workbench.hub.status.observe_entry', return_value={'power': 'starting'}):
            items = projects.power_off_all([self.entry], lambda pending: True)
        self.assertEqual(items[0].status, projects.FAILED)
        self.assertTrue(items[0].failed)


if __name__ == '__main__':
    unittest.main()
