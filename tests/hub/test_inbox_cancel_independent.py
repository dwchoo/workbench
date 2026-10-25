"""TI-13 independent verification of the user's cancel path of the Task Inbox (T6: real backend + fake OMPs + real Hub).

Sources: docs/features/task-inbox SPEC "취소"/"취소 주체"/"Task 동기화"/"inbox_cancel", BRIEF D-20/D-33/D-35/D-37, P2-AC-25..27,
30, 41, ticket TI-13 "독립 초점", docs/features/command-stop (user stop: ``requested_by: user``, SIGINT -> SIGTERM ->
SIGKILL, signals to the command only). Not the implementer's tests.

Seam: the ``ProjectSandbox`` of ``test_project_control`` (temp HOME/XDG, a short private ``XDG_RUNTIME_DIR``,
``WORKBENCH_HUB_AUTOSTART=0``, the fake OMP of ``tests/recovery_boot``), a real Hub (``wb hub start``) with its web
listener and ``/mcp``, and the worker's terminal command typed into the worker pane as a model's tool call would
(``tool terminal {...}``). The command is a host ``sleep``.

Independent oracles: ``/proc`` (the command pid, the backend pid + start ticks, pane pids), the command-stop records in
the project's ``.jsonl`` journals (``requested_by``, signal names), ``tasks.sqlite3`` and the inbox file read with
stdlib ``sqlite3``, ``$FAKE_NOTICES``/``$FAKE_FRAMES``, and an unrelated ``sleep`` this file starts in its own session
(it must survive every stop). Processes this file starts are killed by exact pid + start ticks. Run with
``discover -s tests/hub -p 'test_inbox_cancel_independent.py'``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_inbox_flow_link import LinkCase  # noqa: E402

PASSWORD = 'correct horse battery'
IGNORE_INT = "(trap '' INT; exec sleep {seconds})"  # a command that outlives SIGINT (dies on SIGTERM)
IGNORE_ALL = "(trap '' INT TERM; exec sleep {seconds})"  # a command only SIGKILL ends


def sleeps(seconds: str) -> list[int]:
    found = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            argv = Path(f'/proc/{name}/cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if argv[:2] == [b'sleep', seconds.encode()]:
            found.append(int(name))
    return found


def ticks_of(pid: int) -> int | None:
    try:
        return int(Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def is_alive(pid: int, ticks: int | None = None) -> bool:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[0] not in ('Z', 'X') and (ticks is None or int(fields[19]) == ticks)


class CancelLive(LinkCase):
    def live(self, *titles: str, requesters: tuple[str, ...] = ()):
        """A project with ``titles`` seeded (by the owner, or by each ``requesters`` key in turn), its backend on, the
        real Hub (web + MCP) linked. Returns ``(root, project_id, ids, ui, keys)``."""
        import subprocess as sp
        import time as _time
        from workbench.hub import auth
        from workbench.hub.inbox.service import InboxService, Principal, ProjectRef
        config = Path(self.box.env['XDG_CONFIG_HOME']) / 'omp-workbench'
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        (config / 'hub.toml').write_text('[web]\nbind = "127.0.0.1"\nport = 0\n')
        done = sp.run([sys.executable, '-m', 'workbench', 'hub', 'passwd', '--stdin'], env=self.box.env,
                      cwd=self.box.base, input=PASSWORD + '\n', capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        root, project_id = self.box.project('alpha')
        self.box.register(self.box.entry(root, project_id))
        db = auth.HubDb.open(self.box.hub_state)
        try:
            store = auth.ApiKeyStore(db)
            keys = {'editor': store.issue('ti13-editor', 'editor', {'projects': [project_id]}, None, now=_time.time())}
            for name in requesters:
                keys[name] = store.issue(name, 'requester', {'projects': [project_id]}, None, now=_time.time())
        finally:
            db.close()
        service = InboxService()
        ids: list[str] = []
        try:
            service.set_projects([ProjectRef(project_id, root, root.name)])
            for index, title in enumerate(titles):
                if index < len(requesters):
                    key = keys[requesters[index]]
                    principal = Principal.requester(key.key_id, requesters[index], [project_id])
                else:
                    principal = Principal.owner()
                ids.append(service.submit(principal, project_id, title, f'{title} body', None)['request_id'])
        finally:
            service.close()
        self.box.power_on(root, project_id)
        ui = self.box.attach(root)
        self.box.start_hub()
        self.until(lambda: all(self.row(root, rid)['state'] == 'REVIEWING' for rid in ids), 60,
                   'the seeded requests were delivered')
        self.wait_linked(project_id)
        from fixtures.web_sandbox import tcp_listening, wait_until as web_wait
        pid = json.loads((self.box.hub_state / 'hub.json').read_text())['pid']
        found: list = []
        web_wait(lambda: bool(found.extend(tcp_listening(pid)) or found), 15)
        self.assertEqual(len(found), 1, found)
        self.host, self.port = found[0]
        return root, project_id, ids, ui, keys

    # -- clients -----------------------------------------------------------------------------
    def mcp(self, key):
        from support.mcp_client import McpClient
        client = McpClient(self.host, self.port, key.raw)
        client.initialize()
        client.initialized()
        return client

    def browser(self):
        from test_web_requests_hub import Browser
        browser = Browser(self.host, self.port)
        _status, _headers, page = browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _headers, _ = browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.assertEqual(status, 303)
        return browser

    # -- the worker's command ----------------------------------------------------------------
    def start_command(self, ui, root: Path, rid: str, shell: str, seconds: str) -> tuple[str, int, dict]:
        """Link and start a Task whose worker runs ``shell`` (a ``sleep SECONDS``) in the host shell."""
        answer = self.to_worker(ui, rid)
        self.assertEqual(answer['status'], 'dispatched', answer)
        self.until(lambda: self.worker_tasks(), 30, 'the TASK went to the worker')
        self.until(lambda: self.row(root, rid)['state'] == 'RUNNING', 30)
        from workbench.contracts.ui_v1 import ClientType
        sent = ui.request(ClientType.INPUT, f'tool terminal {json.dumps({"command": shell})}\n'.encode(),
                          pane='worker_omp')
        self.assertTrue(sent.get('ok'), sent)
        pids: list = []
        from test_project_control import wait_until as proc_wait
        proc_wait(lambda: bool(pids.extend(sleeps(seconds)) or pids), 30)
        self.assertEqual(len(pids), 1, pids)
        self.until(lambda: (self.view(root, rid).get('command') or {}).get('command_id'), 30,
                   'the Hub sees the running command')
        return answer['task_id'], pids[0], self.view(root, rid)['command']

    def sentinel(self, seconds: str) -> tuple[int, int]:
        """An unrelated ``sleep`` in its own session; it must survive every stop."""
        proc = subprocess.Popen(['sleep', seconds], start_new_session=True, stdin=subprocess.DEVNULL)
        ticks = None
        for _ in range(200):
            ticks = ticks_of(proc.pid)
            if ticks is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(ticks)

        def clean():
            if proc.poll() is None and is_alive(proc.pid, ticks):  # exact identity
                proc.kill()
            proc.wait(timeout=10)
        self.addCleanup(clean)
        return proc.pid, ticks

    # -- independent readers -----------------------------------------------------------------
    def journal_lines(self, root: Path, needle: str) -> list[dict]:
        found = []
        for path in (root / '.wb').rglob('*.jsonl'):
            for line in path.read_text(errors='replace').splitlines():
                if needle in line:
                    try:
                        found.append(json.loads(line))
                    except ValueError:
                        continue
        return found

    def stop_records(self, root: Path, kind: str = 'requested') -> list[dict]:
        return [item for item in self.journal_lines(root, f'"command_stop_{kind}"')
                if item.get('type') == f'command_stop_{kind}']

    def user_stops(self, root: Path) -> list[dict]:
        return [item for item in self.stop_records(root) if item.get('requested_by') == 'user']

    def flow_cancels(self, root: Path) -> list[dict]:
        return [item for item in self.journal_lines(root, '"cancel_requested"') if item.get('type') == 'cancel_requested'
                and 'task_id' in item]

    def task_closed(self, root: Path, task_id: str) -> list[dict]:
        """The Task ledger's ``task_closed`` records of ``task_id`` (``tasks-flow.jsonl``)."""
        return [item for item in self.journal_lines(root, '"task_closed"')
                if item.get('type') == 'task_closed' and item.get('task_id') == task_id]

    def processes(self, root: Path) -> dict[str, tuple[int, int]]:
        """The backend and every pane process the status snapshot names (pid, start ticks)."""
        pid, ticks, _record = self.box.backend_ref(root)
        found = {'backend': (pid, ticks)}
        snapshot = self.box.status(root)
        for name, value in self.box.pane_processes(snapshot).items():
            found[name] = value
        return found

    def assert_survive(self, processes: dict[str, tuple[int, int]]) -> None:
        for name, (pid, ticks) in processes.items():
            self.assertTrue(is_alive(pid, ticks), f'{name} ({pid}) must survive the stop of the command')

    def observe_until_closed(self, root: Path, rid: str, pid: int, timeout: float) -> list[tuple[str, bool]]:
        """Sample (request state, command alive) until the request is CLOSED. The row is read before the pid is
        looked at, so a CLOSED sample with a live pid proves the request closed before the command ended."""
        samples: list[tuple[str, bool]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.row(root, rid)['state']
            samples.append((state, is_alive(pid)))
            if state == 'CLOSED':
                return samples
            time.sleep(0.1)
        return samples


class McpUserStopTests(CancelLive):
    def test_a_confirmed_cancel_stops_only_the_command_and_the_request_waits_for_its_end(self):
        root, _project_id, (rid,), ui, keys = self.live('Run the long job')
        outsider = self.sentinel('719.9')
        task_id, pid, command = self.start_command(ui, root, rid, IGNORE_INT.format(seconds='719.1'), '719.1')
        self.assertEqual(command['label'], IGNORE_INT.format(seconds='719.1'))
        survivors = self.processes(root)
        client = self.mcp(keys['editor'])
        first = client.tool('inbox_cancel', request_id=rid)
        self.assertEqual(first['status'], 'confirm_required', first)
        self.assertEqual((first['impact']['task_id'], first['impact']['command']['command_id']),
                         (task_id, command['command_id']))
        self.assertTrue(is_alive(pid), 'P2-AC-41: the first call stops nothing')
        self.assertEqual((self.user_stops(root), self.flow_cancels(root)), ([], []))
        done = client.tool('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'], reason='enough')
        self.assertEqual(done['status'], 'cancel_requested', done)
        self.assertEqual(done['stop'], {'status': 'stopping', 'command_id': command['command_id']})
        self.assertNotIn(done['status'], ('cancelled', 'closed'), 'P2-AC-30: acceptance is not the end')
        again = client.call('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        self.assertTrue(again['isError'], 'the same token twice')
        self.assertEqual(again['structuredContent']['error']['code'], 'token_mismatch', again)
        samples = self.observe_until_closed(root, rid, pid, 60)
        self.assertEqual(samples[-1][0], 'CLOSED', samples[-5:])
        self.assertFalse(is_alive(pid), 'the command had ended when the request closed')
        self.assertTrue(all(not (state == 'CLOSED' and alive) for state, alive in samples),
                        'the request was never CLOSED while the command lived')
        self.assertTrue(any(state == 'RUNNING' and alive for state, alive in samples[:-1]),
                        'it was observed open while the command was still being stopped (SIGINT is ignored)')
        row = self.row(root, rid)
        self.assertEqual((row['state'], row['outcome'], row['cancel_state']), ('CLOSED', 'cancelled', None))
        (requested,) = self.user_stops(root)
        self.assertEqual(requested['command_id'], command['command_id'])
        self.assertEqual([item['requested_by'] for item in self.stop_records(root)], ['user'], 'one stop, by the user')
        signals = [item['signal'] for item in self.stop_records(root, 'signal')
                   if item['command_id'] == command['command_id']]
        self.assertEqual(signals[:1], ['SIGINT'], signals)
        self.assertEqual([n for n in ('SIGINT', 'SIGTERM', 'SIGKILL') if n in signals], signals, 'in order')
        self.assertEqual([(item['task_id'], item['by']) for item in self.flow_cancels(root)], [(task_id, 'user')])
        self.assertEqual([item.get('reason') for item in self.task_closed(root, task_id)], ['cancelled'])
        self.assert_survive(survivors)
        self.assertTrue(is_alive(*outsider), 'an unrelated process in its own session is never signalled')
        late = client.call('inbox_cancel', request_id=rid)
        self.assertTrue(late['isError'], 'a closed request cannot be cancelled again')
        self.assertEqual(len(self.user_stops(root)), 1)
        self.until(lambda: [n for n in self.inbox_notices() if n['request_id'] == rid and n['event'] == 'cancelled'],
                   40, 'the manager is told of the cancel once')

    def test_a_requester_may_cancel_only_its_own_request(self):
        root, _project_id, (rid,), ui, keys = self.live('Mine', requesters=('req-a', 'req-b'))
        task_id, pid, command = self.start_command(ui, root, rid, IGNORE_INT.format(seconds='719.2'), '719.2')
        other = self.mcp(keys['req-b'])
        for attempt in (lambda: other.call('inbox_cancel', request_id=rid),
                        lambda: other.call('inbox_cancel', request_id=rid, confirm_token='x' * 32)):
            denied = attempt()
            self.assertTrue(denied['isError'], denied)
            self.assertIn(denied['structuredContent']['error']['code'], ('not_found', 'forbidden'), denied)
        self.assertEqual((self.user_stops(root), self.flow_cancels(root)), ([], []), 'a foreign key stops nothing')
        self.assertTrue(is_alive(pid))
        self.assertEqual(self.row(root, rid)['cancel_state'], None)
        own = self.mcp(keys['req-a'])
        first = own.tool('inbox_cancel', request_id=rid)
        self.assertEqual(first['status'], 'confirm_required', first)
        stolen = other.call('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        self.assertTrue(stolen['isError'], 'a token issued to another key is no use')
        self.assertEqual(self.user_stops(root), [])
        done = own.tool('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        self.assertEqual((done['status'], done['stop']['command_id']), ('cancel_requested', command['command_id']))
        self.until(lambda: self.row(root, rid)['state'] == 'CLOSED', 60, 'the owner of the request cancelled it')
        self.assertFalse(is_alive(pid))
        self.assertEqual(len(self.user_stops(root)), 1)


class WebUserStopTests(CancelLive):
    def test_the_web_confirmation_stops_once_and_never_says_cancelled_before_the_end(self):
        root, _project_id, (rid,), ui, _keys = self.live('Web job')
        task_id, pid, command = self.start_command(ui, root, rid, IGNORE_INT.format(seconds='719.3'), '719.3')
        browser = self.browser()
        status, _headers, page = browser.request('GET', f'/requests/{rid}/cancel')
        self.assertEqual(status, 200, page)
        self.assertIn(task_id, page)
        self.assertIn('719.3', page, 'the confirmation shows the command that will be stopped')
        self.assertTrue(is_alive(pid), 'the confirmation page stops nothing')
        fields = {name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1)
                  for name in ('csrf', 'base_version', 'token')}
        status, headers, _ = browser.request('POST', f'/requests/{rid}/cancel', fields)
        self.assertEqual(status, 303)
        self.assertIn('notice=cancel_stopping', headers['Location'])
        _status, _headers, detail = browser.request('GET', f'/requests/{rid}')
        self.assertIn('취소 요청됨', detail)
        self.assertNotIn('요청을 취소했습니다', detail, 'accepted is not cancelled (P2-AC-30)')
        second, _headers, body = browser.request('POST', f'/requests/{rid}/cancel', fields)
        self.assertEqual(second, 409, 'the same confirmation twice is refused')
        self.assertNotIn('요청을 취소했습니다', body)
        self.assertEqual(len(self.user_stops(root)), 1, 'one stop however often the form is posted')
        self.until(lambda: self.row(root, rid)['state'] == 'CLOSED', 60)
        self.assertFalse(is_alive(pid))
        self.assertEqual(self.row(root, rid)['outcome'], 'cancelled')
        self.assertEqual(len(self.user_stops(root)), 1)
        self.assertEqual([(item['task_id'], item['by']) for item in self.flow_cancels(root)], [(task_id, 'user')])


class RaceAndFailureTests(CancelLive):
    def test_a_manager_cancel_between_confirm_and_token_refuses_the_token_and_stops_nothing(self):
        root, _project_id, (rid,), ui, keys = self.live('Racy job')
        task_id, pid, command = self.start_command(ui, root, rid, IGNORE_INT.format(seconds='719.4'), '719.4')
        client = self.mcp(keys['editor'])
        first = client.tool('inbox_cancel', request_id=rid)
        self.assertEqual(first['status'], 'confirm_required', first)
        cancelled = self.tool(ui, 'to_worker', {'kind': 'work', 'message': 'stop it', 'task_id': task_id,
                                                'cancel': True})
        self.assertIn(cancelled['status'], ('cancelled', 'cancel_requested'), cancelled)
        refused = client.call('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        self.assertTrue(refused['isError'], refused)
        self.assertEqual(refused['structuredContent']['error']['code'], 'token_mismatch', refused)
        self.assertEqual(self.user_stops(root), [], 'no user stop')
        self.assertEqual([item['by'] for item in self.flow_cancels(root)], ['manager'], 'only the manager cancelled')
        row = self.row(root, rid)
        self.assertEqual((row['state'], row['cancel_state']), ('RUNNING', None))
        self.assertTrue(is_alive(pid), 'the manager cancel leaves the command and the user did not stop it')
        # the request stays open for the manager (P2-AC-09); the user may still cancel it afterwards
        self.until(lambda: self.view(root, rid).get('closed') is True, 30, 'the Hub saw the Task closed')
        self.assertEqual(self.row(root, rid)['state'], 'RUNNING')
        second = client.tool('inbox_cancel', request_id=rid)
        self.assertEqual(second['status'], 'confirm_required', second)
        done = client.tool('inbox_cancel', request_id=rid, confirm_token=second['confirm_token'])
        self.assertEqual(done['status'], 'cancel_requested', done)
        self.until(lambda: self.row(root, rid)['state'] == 'CLOSED', 60, 'the user can still end the request')
        self.assertFalse(is_alive(pid), 'the closed request does not leave its command running')
        self.assertEqual(self.row(root, rid)['outcome'], 'cancelled')

    def test_a_stopped_backend_refuses_before_any_change_and_an_unanswered_one_is_never_a_success(self):
        root, project_id, (rid,), ui, keys = self.live('Backend job')
        task_id, pid, command = self.start_command(ui, root, rid, IGNORE_INT.format(seconds='719.5'), '719.5')
        client = self.mcp(keys['editor'])
        backend_pid, backend_ticks, _record = self.box.backend_ref(root)
        first = client.tool('inbox_cancel', request_id=rid)
        before = self.row(root, rid)
        # (1) the backend does not answer: stopped by the test (SIGSTOP on its exact pid + start ticks)
        self.assertTrue(is_alive(backend_pid, backend_ticks))
        os.kill(backend_pid, signal.SIGSTOP)
        resumed = []

        def resume():
            if not resumed and is_alive(backend_pid, backend_ticks):
                os.kill(backend_pid, signal.SIGCONT)
            resumed.append(True)
        self.addCleanup(resume)
        silent = client.call('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        row = self.row(root, rid)
        if silent['isError']:  # nothing could be sent (the hello never completes): refused, the request reverted
            self.assertEqual(silent['structuredContent']['error']['code'], 'backend_unreachable', silent)
            self.assertEqual((row['state'], row['cancel_state']), ('RUNNING', None), 'a refusal leaves no cancel behind')
            reverted = True
        else:  # sent and not answered: surfaced as unknown, never as a success
            result = silent['structuredContent']
            self.assertEqual((result['status'], result.get('cancel')), ('cancel_requested', 'unknown'), silent)
            self.assertEqual((row['state'], row['cancel_state']), ('RUNNING', 'requested'))
            self.assertIn('알 수 없', ' '.join(body for kind, body in self.comments(root, rid) if kind == 'system'))
            reverted = False
        self.assertEqual(self.user_stops(root), [], 'nothing was stopped while the backend was silent')
        resume()
        if reverted:  # a new cancel may follow; it must work and end the request
            retry = client.tool('inbox_cancel', request_id=rid)
            self.assertEqual(retry['status'], 'confirm_required', retry)
            done = client.tool('inbox_cancel', request_id=rid, confirm_token=retry['confirm_token'])
            self.assertEqual((done['status'], done['stop']['command_id']), ('cancel_requested', command['command_id']))
            self.until(lambda: self.row(root, rid)['state'] == 'CLOSED', 60)
            self.assertFalse(is_alive(pid))
            self.assertEqual(len(self.user_stops(root)), 1)
            return
        # whatever the resumed backend then does, a CLOSED request must mean an ended command and a closed Task
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            state = self.row(root, rid)['state']
            alive_now = is_alive(pid)
            if state == 'CLOSED':
                self.assertFalse(alive_now, 'CLOSED while the command lived')
                self.assertTrue(self.task_closed(root, task_id))
                break
            time.sleep(0.2)
        self.assertGreaterEqual(self.row(root, rid)['version'], before['version'])

    def test_a_backend_that_is_off_refuses_a_running_cancel_and_leaves_the_request_unchanged(self):
        root, project_id, (rid,), ui, keys = self.live('Off job')
        task_id, pid, command = self.start_command(ui, root, rid, IGNORE_INT.format(seconds='719.6'), '719.6')
        client = self.mcp(keys['editor'])
        first = client.tool('inbox_cancel', request_id=rid)
        self.assertEqual(first['status'], 'confirm_required', first)
        backend_pid, backend_ticks, _record = self.box.backend_ref(root)
        done = self.box.wb('shutdown', '--yes', cwd=root, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.until(lambda: not is_alive(backend_pid, backend_ticks), 30, 'the backend ended')
        before = self.row(root, rid)
        refused = client.call('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        self.assertTrue(refused['isError'], refused)
        self.assertIn(refused['structuredContent']['error']['code'], ('backend_not_running', 'token_mismatch'), refused)
        after = self.row(root, rid)
        self.assertEqual((after['state'], after['cancel_state'], after['version']),
                         (before['state'], None, before['version']), 'the request is unchanged')
        self.assertEqual(self.user_stops(root), [])
        self.assertEqual([item for item in self.flow_cancels(root) if item.get('by') == 'user'], [])
        fresh = client.call('inbox_cancel', request_id=rid)
        if fresh['isError']:
            self.assertIn(fresh['structuredContent']['error']['code'], ('backend_not_running',), fresh)
        else:  # the first call may issue a token; the confirmation is then refused
            retried = client.call('inbox_cancel', request_id=rid, confirm_token=fresh['structuredContent']['confirm_token'])
            self.assertTrue(retried['isError'], retried)
            self.assertEqual(retried['structuredContent']['error']['code'], 'backend_not_running', retried)
        self.assertEqual(self.row(root, rid)['cancel_state'], None)
        self.assertEqual(self.row(root, rid)['state'], before['state'])

    def test_a_backend_restart_during_a_user_cancel_never_closes_the_request_over_a_live_command(self):
        """The backend (SIGKILL) dies while the user's stop of the command is under way; a command that outlives the
        backend is orphaned and the restarted backend no longer lists it. The request must not close over it."""
        root, project_id, (rid,), ui, keys = self.live('Restart job')
        task_id, pid, command = self.start_command(ui, root, rid, IGNORE_ALL.format(seconds='719.7'), '719.7')
        sleep_ticks = ticks_of(pid)
        self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if is_alive(pid, sleep_ticks) else None)
        client = self.mcp(keys['editor'])
        first = client.tool('inbox_cancel', request_id=rid)
        done = client.tool('inbox_cancel', request_id=rid, confirm_token=first['confirm_token'])
        self.assertEqual(done['status'], 'cancel_requested', done)
        backend_pid, backend_ticks, _record = self.box.backend_ref(root)
        grants = self.grants(project_id)
        self.assertTrue(is_alive(backend_pid, backend_ticks))
        os.kill(backend_pid, signal.SIGKILL)  # the backend this test started, by its exact pid + start ticks
        self.until(lambda: not is_alive(backend_pid, backend_ticks), 20, 'the backend died')
        time.sleep(3.0)  # the Hub keeps syncing: a dead backend must not close the request
        mid = self.row(root, rid)
        if mid['state'] == 'CLOSED':
            self.assertFalse(is_alive(pid, sleep_ticks), 'CLOSED over a live command while the backend was down')
        self.box.power_on(root, project_id)
        self.wait_linked(project_id, grants + 1)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and self.row(root, rid)['state'] != 'CLOSED':
            time.sleep(0.3)
        row = self.row(root, rid)
        if row['state'] == 'CLOSED':
            # SPEC 취소: the request closes only after the Task closed AND its command ended (P2-AC-30)
            self.assertFalse(is_alive(pid, sleep_ticks), 'the request was closed while its command still ran')
            self.assertEqual(row['outcome'], 'cancelled')
            self.assertTrue(self.task_closed(root, task_id))
        else:
            self.assertIn(row['cancel_state'], ('requested', 'stop_requested'),
                          'a request that stays open still says a cancel is under way')


class HubPeerGateTests(CancelLive):
    def test_cancel_task_is_refused_to_every_peer_but_the_verified_hub(self):
        from workbench.backend.client import UiClient
        from workbench.contracts.ui_v1 import ClientType
        root, _project_id, (rid,), ui, _keys = self.live('Gate job')
        task_id, pid, command = self.start_command(ui, root, rid, IGNORE_INT.format(seconds='719.8'), '719.8')
        backend_pid, backend_ticks, record = self.box.backend_ref(root)
        expect = {'status': 'running', 'command_id': command['command_id']}
        fields = dict(task_id=task_id, request_id=rid, expect=expect, reason=None)
        # the attached UI (the user's screen) and a connection that claims the Hub's name from this test process
        for name, attach in (('hb07-ui', True), ('workbench-hub', False), ('workbench-hub', True)):
            with self.subTest(name=name, attach=attach):
                client = UiClient(Path(record['ui_socket']), name=name, timeout=10,
                                  expected_process={'pid': backend_pid, 'start_ticks': backend_ticks})
                try:
                    if attach:
                        try:
                            client.attach((30, 120))
                        except Exception:
                            continue  # the screen is already attached elsewhere: that connection is refused anyway
                    answer = client.request(ClientType.CANCEL_TASK, **fields)
                finally:
                    client.close()
                self.assertIs(answer.get('ok'), False, answer)
                self.assertEqual(answer.get('reason'), 'not_attached', answer)
        self.assertTrue(is_alive(pid))
        self.assertEqual((self.user_stops(root), self.flow_cancels(root)), ([], []))
        self.assertEqual(self.task_closed(root, task_id), [])


if __name__ == '__main__':
    unittest.main()
