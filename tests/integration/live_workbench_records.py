"""WR-07: one noneditable installed WB flow, real OMP and localhost scripted replies.

The provider chooses literal actions, not natural-language intent. WR-06's actual
startup/switch/stale/pause controls are reused; this checks the complete WB path.
"""
from __future__ import annotations

from hashlib import sha256
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import unittest
from uuid import uuid4

from tests.backend.installation import live_project_sessions as sessions
from tests.integration import cw16_harness as h

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / '.workflow/workbench-records/I025'
OMP = '/home/dwchoo/.local/bin/omp'
CACHE = Path('/home/dwchoo/ai-exp-assistant/.workflow/WI-02-independent-tests/git-install-fu6zu538/uv-cache')


def arguments(action, **changes):
    value = dict(action=action, record_id=None, revision=None, expected_revision=None,
                 title=None, body=None, task_id=None, query=None, limit=None,
                 after=None, output_path=None)
    value.update(changes)
    return value


def content(message):
    value = message.get('content', '')
    return value if isinstance(value, str) else ''.join(x.get('text', '') for x in value if isinstance(x, dict))


class RecordsProvider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.connection.settimeout(5)
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.loads(raw)
        owner = self.server.owner
        names = [x.get('function', {}).get('name') for x in body.get('tools', [])]
        role = 'manager' if 'to_worker' in names else 'worker' if 'to_manager' in names else 'unknown'
        with owner.provider_lock:
            self.server.requests += 1
            index = self.server.requests
            (owner.output / f'provider-{index:03d}-{role}.json').write_bytes(raw)
            last = body.get('messages', [{}])[-1]
            phase = owner.active.get(role)
            if last.get('role') == 'tool' and phase:
                answer = json.loads(content(last))
                owner.results[phase] = answer
                (owner.output / (phase + '.done.json')).write_text(json.dumps(answer, ensure_ascii=False))
                owner.active.pop(role, None)
                turn = h.text('WR07_DONE_' + phase)
            else:
                text = content(last)
                phase = next((token for token in owner.plans if token in text), None)
                if phase:
                    expected_role, args = owner.plans[phase]
                    assert role == expected_role, (role, phase, names)
                    assert 'workbench_records' in names, names
                    owner.active[role] = phase
                    owner.contexts[phase] = body
                    turn = h.tools(('workbench_records', {'i': 'explicit owned record request', **args}, 'wr07-' + uuid4().hex))
                else:
                    turn = h.text('WR07 auxiliary fixture acknowledged')
        payload = b''.join(h.ScriptedProvider._frames(turn))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


TURN = r'''
import json,sys,time
from pathlib import Path
from workbench.backend.client import UiClient
from workbench.contracts.v1 import PaneId
project,role,token,done,screen=map(str,sys.argv[1:])
record=json.loads((Path(project)/'.wb/backend.json').read_text())
with UiClient(Path(record['ui_socket']),expected_process=record['processes']['backend']) as c:
    assert c.attach()['ok']
    pane=PaneId.MANAGER_OMP if role=='manager' else PaneId.WORKER_OMP
    assert c.input(pane,token.encode())['ok']
    assert c.input(pane,b'\r')['ok']
    deadline=time.monotonic()+35
    while not Path(done).exists() and time.monotonic()<deadline:c.pump(.1)
    assert Path(done).exists(), 'provider/tool result missing '+token
    # Drain the assistant's final response and the terminal's real UI preview.
    end=time.monotonic()+.4
    while time.monotonic()<end:c.pump(.05)
    Path(screen).write_bytes(b''.join(frame.payload for frame in c.displays))
    c.detach()
print(json.dumps({'token':token,'result':json.loads(Path(done).read_text())}))
'''

INSPECT = r'''
from hashlib import sha256
import importlib.metadata,json,sys
from pathlib import Path
import workbench
from workbench.assets import canonical_path
paths=['omp_bridge/g3/bridge.ts','omp_bridge/g3/records_tool.ts','omp_bridge/g3/records_context.ts','omp_bridge/contract/records_v1.ts','src/workbench/backend/records_service.py','src/workbench/backend/launcher.py','src/workbench/backend/migrate.py']
print(json.dumps({'module':workbench.__file__,'python':sys.executable,'sys_path':sys.path,
'direct_url':json.loads(importlib.metadata.distribution('workbench').read_text('direct_url.json')),
'assets':{p:{'path':str(canonical_path(p)),'sha256':sha256(canonical_path(p).read_bytes()).hexdigest()} for p in paths}}))
'''

PRESERVE = r'''
from hashlib import sha256
import json,os,sys
from pathlib import Path
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import summary_input_key
from workbench.storage.important_log import ImportantLogCatalog,ImportantLogReader
from workbench.storage.work_records.store import RecordStore
from workbench.tasks.repository import TaskRepository
root=Path(sys.argv[1]);request,result=json.loads(sys.argv[2]);record_id=sys.argv[3]
records=root/'records.sqlite3';before=sha256(records.read_bytes()).hexdigest()
catalog=ImportantLogCatalog(root/'important-logs',create=True)
request['dbHealth']=catalog.health_snapshot()['active']
request['inputKey']=summary_input_key(request['subject'],request['revision'],request['sources'],request['sourceGaps'],request['skillDigest'])
result['inputKey']=request['inputKey'];result['facts']=['WR07 automatic summary sentinel']
with TaskRepository(root/'tasks.sqlite3') as repo:
    os.chmod(root/'tasks.sqlite3',0o600)
    repo.reserve_summary_request(request);repo.record_summary_state(request['requestId'],'sending')
outcome=store_summary(root,request['requestId'],result);assert outcome['saved'],outcome
entries=ImportantLogReader(root/'important-logs').show(request['subject'])['entries']
assert entries[0]['entry']['body']['facts']==['WR07 automatic summary sentinel']
maintenance=catalog.maintenance_step()
assert maintenance['state'] in ('ok','maintenance_incomplete'),maintenance
assert sha256(records.read_bytes()).hexdigest()==before
store=RecordStore(records)
print(json.dumps({'writer':outcome,'maintenance':maintenance,'records_sha256_before':before,
'records_sha256_after':sha256(records.read_bytes()).hexdigest(),'first':store.get(record_id,1),'latest':store.get(record_id)}))
'''


class RecordsInstalledRun(sessions.InstalledRun):
    def __init__(self, output):
        super().__init__(output, omp=OMP)
        self.provider.RequestHandlerClass = RecordsProvider
        self.provider.owner = self
        self.provider_lock = threading.Lock()
        self.plans = {}
        self.active = {}
        self.results = {}
        self.contexts = {}
        self.observed_processes = {}

    def install(self):
        self.env.update(UV_OFFLINE='1', UV_NO_PROGRESS='1', UV_LINK_MODE='copy')
        shutil.copytree(CACHE, Path(self.env['UV_CACHE_DIR']), dirs_exist_ok=True)
        super().install()
        # Keep every built-in model role on the localhost fixture too.
        roles = ', '.join(f'{name}: wi-project/scripted:minimal' for name in
                          ('default','slow','plan','smol','task','tiny','memory','commit'))
        self.model.write_text(self.model.read_text() + f'modelRoles: {{{roles}}}\n'
            'tools:\n  intentTracing: false\nexternalThinking: false\nprewalk:\n  enabled: false\n')
        _, stdout, _ = self.run([self.python, '-I', '-c', INSPECT], 'installed-identities')
        identity = json.loads(stdout)
        assert not identity['direct_url'].get('dir_info', {}).get('editable', False)
        assert not any(str(path).startswith(str(ROOT)) for path in identity['sys_path'])
        assert Path(identity['module']).is_relative_to(self.private)
        for name, item in identity['assets'].items():
            assert item['sha256'] == sha256((ROOT / name).read_bytes()).hexdigest(), name
        self.report['installed_identity'] = identity
        self.report['omp'] = {'path': str(self.omp), 'sha256': sha256(self.omp.read_bytes()).hexdigest()}
        shutil.copy2(next((self.private / 'dist').glob('*.whl')), self.output / 'candidate.whl')

    def observe_owned(self):
        self.observed_processes.update(self.private_processes())
        self.report['observed_process_startticks'] = self.observed_processes

    def turn(self, project, label, role, action, **changes):
        token = 'WR07_' + label
        self.plans[token] = (role, arguments(action, **changes))
        done = self.output / (token + '.done.json')
        _, stdout, _ = self.run([self.python, '-I', '-c', TURN, project, role, token, done,
                                self.output / (token + '-ui.raw')], token, cwd=project, timeout=45)
        result = json.loads(stdout)['result']
        self.observe_owned()
        return result, self.contexts[token]

    def shutdown(self, project, label):
        self.observe_owned()
        self.run([self.command, 'shutdown', '--yes', '--json'], 'shutdown-' + label, cwd=project, timeout=65)


def run_installed_flow(output):
    run = RecordsInstalledRun(output)
    first = second = None
    try:
        print('WR07 offline local build/install', flush=True)
        run.install()
        project, _ = run.project('research-A')
        run.start(project, 'start-A', timeout=90)
        run.observe_owned()
        status = run.status(project, 'status-A')
        assert status['phase'] == 'ready', status
        assert status['omp_isolation']['ok'], status['omp_isolation']
        first_body = '# WR07_A_original\n"한글" **literal % _** 😀\n' + 'x' * 180 + '\nWR07_A_FULL_SENTINEL\n'
        result, body = run.turn(project, 'A_CREATE', 'manager', 'create', title='WR07_A_first', body=first_body)
        assert result['status'] == 'saved', result
        first = result['record']; record_id = first['record_id']
        assert first['body'] == first_body and first['actor'] == 'manager'
        result, _ = run.turn(project, 'A_CROSS_ROLE', 'worker', 'show', record_id=record_id)
        assert result == {'status':'ok','record':first}, result
        latest_body = 'WR07_A_revision2_preview % _ ' + 'y' * 200 + '\nWR07_A_FULL_V2\n'
        result, _ = run.turn(project, 'A_REVISE', 'worker', 'revise', record_id=record_id,
                             expected_revision=1, title='WR07_A_revision2', body=latest_body)
        assert result['status'] == 'saved' and result['record']['revision'] == 2, result
        second = result['record']; assert second['actor'] == 'worker' and second['body'] == latest_body
        run.shutdown(project, 'A-before-restart')
        print('WR07 summary writer/maintenance and process restart', flush=True)
        from tests.backend.test_summary_adapter_independent import contract_fixture
        request, _, summary = contract_fixture(('work',))
        _, stdout, _ = run.run([run.python, '-I', '-c', PRESERVE, project / '.wb',
            json.dumps([request, summary]), record_id], 'summary-maintenance', cwd=project)
        preserved = json.loads(stdout)
        assert preserved['first'] == {'status':'ok','record':first}
        assert preserved['latest'] == {'status':'ok','record':second}
        run.report['summary_preservation'] = preserved
        run.start(project, 'restart-A', timeout=90)
        result, context = run.turn(project, 'A_AFTER_RESTART', 'manager', 'show', record_id=record_id)
        assert result == {'status':'ok','record':second}, result
        context_text = json.dumps(context['messages'], ensure_ascii=False)
        assert 'WB 작업 기록 — 참고 데이터' in context_text and 'WR07_A_revision2_preview' in context_text
        assert 'WR07_A_FULL_V2' not in context_text, 'new session must receive preview, not full saved body'
        result, _ = run.turn(project, 'A_SEARCH', 'worker', 'search', query='% _')
        assert [item['record_id'] for item in result['records']] == [record_id], result
        result, _ = run.turn(project, 'A_EXPORT', 'manager', 'export', record_id=record_id,
                             revision=1, output_path='selected-revision.md')
        assert result == {'status':'exported','path':str(project/'selected-revision.md'),
                          'record_id':record_id,'revision':1}, result
        assert (project/'selected-revision.md').read_text().endswith(first_body)
        assert not (project/'docs').exists()
        run.report['flow_A'] = {'record_id':record_id,'first':first,'latest':second,
            'preview_next_provider_context':True,'export':result,'docs_not_created':True}
        print('WR07 second project and role isolation', flush=True)
        project_b, _ = run.project('research-B')
        run.start(project_b, 'start-B', timeout=90)
        result_b, context_b = run.turn(project_b, 'B_CREATE', 'worker', 'create', title='WR07_B_only', body='WR07_B_SEPARATE\n')
        assert result_b['status'] == 'saved' and result_b['record']['actor'] == 'worker'
        result, _ = run.turn(project_b, 'B_READ_A', 'manager', 'show', record_id=record_id)
        assert result['status'] == 'not_found', result
        assert 'WR07_A_revision2_preview' not in json.dumps(context_b['messages'], ensure_ascii=False)
        assert not (project_b/'docs').exists()
        run.report['flow_B'] = {'created':result_b,'project_A_not_found':result}
        run.shutdown(project_b, 'B')
        run.shutdown(project, 'A-final')
        # Public installed CLI reopens after all backend/OMP processes have stopped.
        _, stdout, _ = run.run([run.command, 'records', 'show', record_id, '--revision','1','--json'],
                              'stopped-cli-revision1', cwd=project)
        assert json.loads(stdout) == {'status':'ok','record':first}
        _, stdout, _ = run.run([run.command, 'records', 'show', record_id,'--json'], 'stopped-cli-latest', cwd=project)
        assert json.loads(stdout) == {'status':'ok','record':second}
        run.report['checks'] = {'installed_payload_identity':True,'actual_two_role_record_flow':True,
            'restart_preview_and_fullbody_lookup':True,'literal_search_selected_revision_export':True,
            'two_project_isolation':True,'summary_maintenance_preserves_exact_records_db':True,
            'stopped_cli_reopens_history':True,'no_default_docs_notes':True}
    except BaseException as error:
        run.report['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        # Preserve only diagnosis/proof, never the whole private HOME or native cache.
        for index, project in enumerate(run.projects):
            target = output / ('project-' + str(index))
            target.mkdir(exist_ok=True)
            for name in ('records.sqlite3','tasks.sqlite3','backend.json','backend.log'):
                path = project / '.wb' / name
                if path.is_file():shutil.copy2(path, target / name)
        run.cleanup()
        run.report['observed_processes_final'] = {str(pid):sessions.state(pid,ticks)
            for pid,ticks in run.observed_processes.items()}
        if not run.report['remaining_processes'] and not run.report['unrecorded_private_processes']:
            shutil.rmtree(run.private)
            run.report['private_runtime_removed'] = True
        else:run.report['private_runtime_removed'] = False
        (output/'receipt.json').write_text(json.dumps(run.report,ensure_ascii=False,indent=2))
    return run.report


@unittest.skipUnless(os.environ.get('WB_LIVE_RECORDS') == '1', 'requires explicit owned actual-OMP run')
class InstalledAssetsTests(unittest.TestCase):
    def test_full_installed_record_flow(self):
        output = OUTPUT / ('installed-' + str(time.time_ns()))
        report = run_installed_flow(output)
        self.assertIsNone(report['error'])
        self.assertTrue(all(report['checks'].values()))
        self.assertEqual(report['remaining_processes'], [])
        self.assertEqual(report['unrecorded_private_processes'], {})
        self.assertTrue(report['private_runtime_removed'])
