"""현재 사용자 uv Git 설치의 명시적 유지보수.

설치 전 모든 runner 코드를 로드한다. 자기 venv 교체 뒤에는 현재
프로세스에서 새 패키지를 import하지 않고 새 interpreter로 검증한다.
"""
from __future__ import annotations

from contextlib import ExitStack
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit, urlunsplit, parse_qs
from uuid import uuid4

from .paths import BackendLocked, DataDirError, DataLayout, InstanceLock, write_private_json
from .client import ClientError, UiClient
from .maintenance import discover, fence, require_no_writers
from .process_identity import inspect_backend, process_state
from . import update_backup
from workbench.contracts.ui_v1 import ClientType
from workbench.hub import process as hub_process
from workbench.hub.client import HubClient
from workbench.hub.paths import hub_layout


def command(argv, *, env=None, timeout=180, pass_fds=()):
    """도구의 상속 process group을 unreaped leader로 소유한 동안만 정리한다."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        child = subprocess.Popen(list(map(str, argv)), env=env, stdin=subprocess.DEVNULL,
            stdout=out, stderr=err, start_new_session=True, pass_fds=pass_fds)
        try:
            deadline = time.monotonic() + timeout
            while (status := os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)) is None:
                if time.monotonic() >= deadline:
                    raise DataDirError('업데이트 도구 실행 시간 초과; 설치 상태를 확인하세요.')
                time.sleep(.02)
            code = status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
        finally:
            # waitid retained our child PID, including a zombie; the group ID
            # cannot be reused before this signal. Do not signal after wait().
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(argv, code, out.read().decode(errors='replace'),
                                           err.read().decode(errors='replace'))


def checked(argv, *, env=None):
    result = command(argv, env=env)
    if result.returncode:
        # Tool stderr/argv may contain source credentials. Never print them.
        raise DataDirError(f'업데이트 준비 실패 ({Path(argv[0]).name} exit {result.returncode}); 종료·설치는 진행하지 않습니다.')
    return result.stdout.strip()


def safe_url(url):
    value = urlsplit(url)
    host = value.hostname or ''
    if ':' in host:
        host = '[' + host + ']'
    if value.port:
        host += ':' + str(value.port)
    return urlunsplit((value.scheme, host, value.path, '', ''))


def installation():
    try:
        distribution = importlib.metadata.distribution('workbench')
        source = json.loads(distribution.read_text('direct_url.json') or '{}')
        vcs = source.get('vcs_info', {})
        if distribution.read_text('INSTALLER').strip() != 'uv' or vcs.get('vcs') != 'git':
            raise ValueError('unsupported source')
        if source.get('dir_info', {}).get('editable') or not re.fullmatch('[0-9a-f]{40,64}', vcs.get('commit_id', '')):
            raise ValueError('editable/unknown commit')
        url = source['url']
        parsed = urlsplit(url)
        if parsed.scheme not in ('https', 'http', 'ssh', 'file') or parsed.query or parsed.fragment:
            raise ValueError('unsupported source URL')
        prefix = Path(sys.prefix)
        if (not Path(distribution.locate_file('')).resolve().is_relative_to(prefix.resolve())
                or Path(sys.executable) != prefix / 'bin/python'):
            raise ValueError('distribution/interpreter mismatch')
        receipt = tomllib.loads((prefix/'uv-receipt.toml').read_text())['tool']
        requirement, = receipt['requirements']
        git = urlsplit(requirement['git'])
        if requirement['name'] != 'workbench' or urlunsplit(git._replace(query='', fragment='')) != url:
            raise ValueError('receipt source mismatch')
        requested = vcs.get('requested_revision')
        revisions = parse_qs(git.query)
        if requested and requested not in sum(revisions.values(), []):
            raise ValueError('receipt ref mismatch')
        uv = shutil.which('uv')
        if not uv or Path(checked([uv, 'tool', 'dir'])).resolve() / 'workbench' != prefix.resolve():
            raise ValueError('uv tool/interpreter mismatch')
        return {'url':url, 'previous':vcs['commit_id'], 'uv':uv, 'prefix':str(prefix),
                'python':str(getattr(sys, '_base_executable', sys.executable))}
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DataDirError('업데이트에는 확인된 uv 관리 Git 설치가 필요합니다. 개발 checkout은 자동 전환하지 않습니다.') from exc


def prepare(install, ref, stage):
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', ref)
            or '..' in ref or ref.endswith(('/', '.', '.lock')) or '//' in ref):
        raise DataDirError('지원하지 않는 Git ref입니다 (branch/tag/commit 필요).')
    git = shutil.which('git')
    if not git:
        raise DataDirError('Git 실행 파일이 필요합니다.')
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    source = stage/'source'
    checked([git, 'clone', '--no-checkout', '--filter=blob:none', '--', install['url'], source], env=environment)
    # Clone local branches contain only the default branch; remote branches and
    # peeled annotated tags are resolved explicitly, never as option/revision syntax.
    candidates = [ref] if re.fullmatch('[0-9a-f]{40,64}', ref) else ['refs/heads/'+ref, 'refs/remotes/origin/'+ref, 'refs/tags/'+ref]
    commits = set()
    for candidate in candidates:
        result = command([git, '-C', source, 'rev-parse', '--verify', '--end-of-options', candidate+'^{commit}'], env=environment)
        if result.returncode == 0:
            commits.add(result.stdout.strip())
    if len(commits) != 1 or not re.fullmatch('[0-9a-f]{40,64}', next(iter(commits), '')):
        raise DataDirError('Git ref가 없거나 모호합니다; 종료·설치는 진행하지 않습니다.')
    target = commits.pop()
    staged = dict(environment, UV_TOOL_DIR=str(stage/'tools'), UV_TOOL_BIN_DIR=str(stage/'bin'),
                  UV_PYTHON_DOWNLOADS='never')
    checked([install['uv'], 'tool', 'install', '--python', install['python'],
             'git+'+install['url']+'@'+target], env=staged)
    # 과거 ref도 현재 update/start fence를 지켜야 자기 교체 중 새 backend를
    # 열지 않는다. missing OMP를 명시해 미지원 과거 target도 실행하지 않는다.
    probe_data = stage/'start-fence-probe'
    probe = command([stage/'bin/wb', 'start', '--no-attach', '--data-dir', probe_data,
                     '--omp', stage/'missing-omp'], env=staged)
    if (probe.returncode != 1 or '업데이트' not in probe.stderr
            or os.path.lexists(probe_data)):
        raise DataDirError(f'목표 WB의 update/start fence 지원을 확인할 수 없습니다 (start exit {probe.returncode}); 작업을 종료하지 않습니다.')
    return target


VERIFY = '''import importlib.metadata as m,json,sys
+from pathlib import Path
+from workbench.assets import canonical_path
+d=m.distribution("workbench"); s=json.loads(d.read_text("direct_url.json")); expected=json.loads(sys.argv[1])
+ok=(s.get("url")==expected["url"] and s.get("vcs_info",{}).get("commit_id")==expected["commit"]
+ and d.read_text("INSTALLER").strip()=="uv" and sys.prefix==expected["prefix"]
+ and Path(d.locate_file("")).resolve().is_relative_to(Path(sys.prefix).resolve())
+ and not s.get("dir_info",{}).get("editable",False)
+ and all(canonical_path(p).is_file() for p in ("omp_bridge/g3/bridge.ts","omp_bridge/skills/workbench-report/scripts/report_io.py")))
+print(json.dumps({"commit":s.get("vcs_info",{}).get("commit_id"),"verified":ok}))
+raise SystemExit(0 if ok else 1)
+'''.replace('\n+', '\n')


def shutdown(identity, client, pending):
    response = client.request(ClientType.SHUTDOWN_CONFIRM, token=pending['token'])
    if not response.get('ok', True):
        raise DataDirError('backend 종료 동의가 만료되거나 작업 identity가 변경되었습니다.')
    wait = response.get('result_deadline', 60)
    if type(wait) not in (int, float) or not 0 < wait <= 180:
        raise DataDirError('backend 종료 deadline 미확인')
    deadline = time.monotonic() + wait
    while client.closing is None:
        if time.monotonic() > deadline or not client.pump(.2):
            raise DataDirError('backend 종료 결과 미확인; 설치하지 않습니다.')
    result = client.closing.get('result')
    if not isinstance(result, dict) or result.get('verified') is not True:
        raise DataDirError('backend 종료 미검증; 설치하지 않습니다.')
    ref = identity.record['processes']['backend']
    deadline = time.monotonic()+10
    while process_state(ref['pid'], ref['start_ticks'], uid=identity.record['uid'],
                        boot_id=identity.record['boot_id']) == 'alive' and time.monotonic()<deadline:
        time.sleep(.05)
    if process_state(ref['pid'], ref['start_ticks'], uid=identity.record['uid'], boot_id=identity.record['boot_id']) != 'ended':
        raise DataDirError('정확한 backend PID/start ticks의 종료 미확인')
    return result


def run(args):
    try:
        with fence() as descriptor:
            return _run(args, descriptor)
    except DataDirError:
        raise
    except (OSError, ValueError, KeyError, TypeError, ClientError, BackendLocked, subprocess.TimeoutExpired) as error:
        raise DataDirError(f'업데이트 확인 실패 ({type(error).__name__}); 원본·updates manifest와 실행 상태를 확인하세요.') from error


def refuse_managed_caller(identities):
    """자기 caller가 종료 대상의 session/descendant이면 종료 전에 거절한다."""
    running = [item for item in identities if item.state == 'running']
    if not running:
        return
    message = 'WB가 관리하는 shell/terminal 또는 caller identity 미확인 상태에서는 업데이트할 수 없습니다. 외부 terminal에서 다시 실행하세요.'
    try:
        caller = Path('/proc/self/stat').read_bytes().rsplit(b') ', 1)[1].split()
        session = int(caller[3])
        ancestry, pid = {}, os.getpid()
        for _ in range(256):
            fields = (Path('/proc')/str(pid)/'stat').read_bytes().rsplit(b') ', 1)[1].split()
            ancestry[pid] = int(fields[19])
            parent = int(fields[1])
            if parent == 0:
                break
            if parent in ancestry:
                raise ValueError('ancestry cycle')
            pid = parent
        else:
            raise ValueError('ancestry limit')
        current = Path('/proc/self/stat').read_bytes().rsplit(b') ', 1)[1].split()
        if any(caller[index] != current[index] for index in (1, 3, 19)):
            raise ValueError('caller changed')
        for item in running:
            record = item.record
            for ref in record['processes'].values():
                if ref['pid'] == session or ref['pid'] in ancestry:
                    # Exact live identity is unsafe; a matching number with stale
                    # ticks/unknown state is also insufficient to permit shutdown.
                    if (process_state(ref['pid'], ref['start_ticks'], uid=record['uid'], boot_id=record['boot_id']) != 'alive'
                            or (ref['pid'] in ancestry and ancestry[ref['pid']] != ref['start_ticks'])):
                        raise ValueError('managed caller identity unknown')
                    raise DataDirError('WB 관리 shell/terminal 안에서는 업데이트할 수 없습니다. 외부 terminal에서 다시 실행하세요.')
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        raise DataDirError(message) from error


def _hub_version_text(version):
    if not isinstance(version, dict):
        return 'unknown'
    commit, kind = version.get('commit'), version.get('kind')
    return f"{version.get('release')} ({commit[:12] if kind == 'git' and isinstance(commit, str) else kind})"


def hub_to_stop():
    """The Hub ``wb update`` stops (SPEC "`wb update`와의 관계"), or None when none runs.

    ``unknown`` stops the update before any question or backend. A proven Hub that
    does not answer is returned: :func:`hub_process.stop_hub` may still end it by SIGTERM.
    """
    layout = hub_layout()
    try:
        with HubClient(layout, name='workbench-update', timeout=hub_process.RESPONSE_TIMEOUT) as client:
            return client.identity
    except hub_process.HubNotRunning:
        return None
    except hub_process.HubUnresponsive as exc:
        return hub_process.HubIdentity(hub_process.UNKNOWN, layout, exc.record, exc.reason)
    except hub_process.HubUnknown as exc:
        raise DataDirError(f"Hub 상태 불명({exc.reason}, lock {layout.lock})이라 업데이트를 중단합니다; "
                           "어떤 backend도 종료하지 않았습니다. 'wb hub stop'으로 Hub를 멈춘 뒤 다시 실행하세요.") from exc


def hub_stop_notice(identity):
    shown = identity.welcome or identity.record or {}
    return f"Hub(pid {shown.get('pid')}, version {_hub_version_text(shown.get('version'))})를 멈춥니다"


HUB_STOPPED_TEXT = 'Hub는 멈춘 상태입니다. 다음 `wb` 실행 때 다시 시작됩니다.'


def hub_active(identity):
    """Live Hub-owned things (``stop_request.active``, e.g. the Board agent set) for the update question.

    Read once before the question; only a running, answering Hub is asked (an unresponsive one is
    left to :func:`stop_hub_for_update`). Any failure stops the update before any backend.
    """
    if identity is None or identity.state != hub_process.RUNNING:
        return []
    try:
        with HubClient(identity.layout, name='workbench-update', timeout=hub_process.RESPONSE_TIMEOUT) as client:
            offer = client.request('stop_request')
    except hub_process.HubNotRunning:
        return []
    except (hub_process.HubUnknown, hub_process.HubUnresponsive) as exc:
        raise DataDirError(f"Hub 상태를 확인하지 못해({exc.reason}) 업데이트를 중단합니다; 어떤 backend도 종료하지 "
                           "않았습니다. 'wb hub stop'으로 Hub를 멈춘 뒤 다시 실행하세요.") from exc
    active = offer.get('active') if offer.get('ok') else None
    if not isinstance(active, list):
        raise DataDirError("Hub의 정지 확인을 준비할 수 없어 업데이트를 중단합니다; 어떤 backend도 종료하지 않았습니다.")
    return active


def hub_active_confirmed(confirmed):
    """``stop_hub``'s ``ask``: yes only for exactly the Hub-owned things the user confirmed in the
    update question. A set that changed meanwhile is refused and nothing stops (fail-safe)."""
    def ask(active):
        return bool(confirmed) and active == confirmed
    return ask


def stop_hub_for_update(identity, *, announced=False, confirmed=()):
    """Stop the Hub (and the Hub-owned things in ``confirmed``) before any backend; any failure
    stops the update first."""
    if not announced:
        print(hub_stop_notice(identity))
    result = hub_process.stop_hub(hub_active_confirmed(list(confirmed)), identity.layout)
    if result.status not in ('stopped', 'not_running'):
        raise DataDirError(f"Hub(pid {result.pid})를 멈추지 못해 업데이트를 중단합니다 ({result.status}: {result.reason}); "
                           "어떤 backend도 종료하지 않았습니다. 'wb hub stop'으로 Hub를 멈춘 뒤 다시 실행하세요.")
    if result.status == 'stopped' and not _wait_hub_exit(identity.record or {}):
        raise DataDirError(f"Hub(pid {result.pid})가 lock을 놓은 뒤에도 끝나지 않아 업데이트를 중단합니다; 어떤 backend도 "
                           "종료하지 않았습니다. Hub가 끝난 것을 확인한 뒤 다시 실행하세요.")
    return result


# The Hub's own stop budget (Board set: SIGHUP 0.5 s + SIGTERM grace 5 s + SIGKILL/reap) plus a margin.
HUB_EXIT_WAIT = 10.0


def _wait_hub_exit(record):
    """The lock is released last, just before the Hub process exits: wait (bounded) for that exit so no
    backend is touched while the Hub process still runs. Only the exact recorded process is read; False when
    it still runs after :data:`HUB_EXIT_WAIT`."""
    pid, ticks = record.get('pid'), record.get('start_ticks')
    uid, boot = record.get('uid'), record.get('boot_id')
    deadline = time.monotonic() + HUB_EXIT_WAIT
    while True:
        if process_state(pid, ticks, uid=uid, boot_id=boot) != 'alive':
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _run(args, fence_descriptor):
    install = installation()
    with tempfile.TemporaryDirectory(prefix='wb-update-') as temporary, ExitStack() as stack:
        target = prepare(install, args.ref, Path(temporary))
        identities = discover(args)
        refuse_managed_caller(identities)
        hub = hub_to_stop()
        owned = hub_active(hub)  # e.g. the Board agent set: joins the one question below
        projects, live, active = [], [], []
        for identity in identities:
            item = {'data_root':str(identity.layout.root), 'project_root':str(identity.project_root or identity.layout.root.parent),
                    'project_id':identity.project_id, 'snapshot':identity.record, 'shutdown':None}
            projects.append(item)
            if identity.state != 'running':
                continue
            client = stack.enter_context(UiClient(identity.layout.ui_socket, name='workbench-update',
                timeout=10, expected_process=identity.record['processes']['backend']))
            pending = client.request(ClientType.SHUTDOWN_REQUEST)
            if not pending.get('token') or not isinstance(pending.get('active'), list):
                raise DataDirError('backend의 종료 확인을 준비할 수 없습니다.')
            if pending.get('scope_bound') is not True:
                raise DataDirError('이 backend는 표시한 작업에 결합된 종료 동의를 지원하지 않습니다; 작업을 직접 확인·종료한 뒤 다시 업데이트하세요.')
            if any(work.get('kind') in ('previous_survivor','left_session','shell_unknown') or
                   (work.get('kind') == 'omp_turn' and work.get('idle') is None) for work in pending['active']):
                raise DataDirError('생존 process 또는 확인되지 않은 작업이 있습니다; 업데이트를 보류합니다.')
            item['snapshot'] = client.snapshot()
            live.append((identity, client, pending, item))
            if pending['active']:
                active.append({'project':item['project_root'], 'active':pending['active']})
        if active or owned:
            print('업데이트를 위해 다음 작업과 backend·양 역할 OMP·host shell을 종료합니다:')
            if active:
                print(json.dumps(active, ensure_ascii=False, sort_keys=True))
            if owned:
                from workbench.hub.cli import active_text
                print('Hub를 멈추면 다음도 끝납니다:')
                for item in owned:
                    print(f'  - {active_text(item)}')
            if hub is not None:
                # The Hub joins an existing question; it never asks one by itself.
                print(hub_stop_notice(hub))
            try:
                if sys.stdin is None or not sys.stdin.isatty() or input('종료·기록·백업 후 업데이트할까요? [y/N] ').strip().lower() != 'y':
                    print('업데이트 취소: 작업은 유지됩니다.')
                    return 1
            except (EOFError, KeyboardInterrupt, OSError, ValueError):
                print('업데이트 취소: 작업은 유지됩니다.')
                return 1
        state = os.environ.get('XDG_STATE_HOME', '')
        base = (Path(state) if os.path.isabs(state) else Path.home()/'.local/state')/'omp-workbench/updates'
        for item in projects:
            source = Path(item['project_root']).resolve()
            data = Path(item['data_root']).resolve()
            if base.resolve().is_relative_to(source) or base.resolve().is_relative_to(data):
                raise DataDirError('backup root가 프로젝트 source와 겹칩니다.')
        hub_note = ''
        if hub is not None:
            stop_hub_for_update(hub, announced=bool(active or owned), confirmed=owned)
            hub_note = '\n' + HUB_STOPPED_TEXT  # every abort from here on leaves the Hub stopped
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            run = base/uuid4().hex
            run.mkdir(mode=0o700)
            manifest = {'schema_version':1, 'source':safe_url(install['url']), 'previous':install['previous'],
                        'target':target, 'installation':'not_started', 'projects':projects,
                        'backup_complete':False, 'complete':False}
            manifest['manual_reinstall'] = shlex.join([install['uv'], 'tool', 'install', '--force',
                '--python', install['python'], 'git+'+safe_url(install['url'])+'@'+install['previous']])
            write_private_json(run/'manifest.json', manifest)
        except OSError as error:
            if not hub_note:
                raise
            raise DataDirError(f'업데이트 확인 실패 ({type(error).__name__}); 원본·updates manifest와 실행 상태를 확인하세요.'
                               + hub_note) from error
        print(f"업데이트 {install['previous']} -> {target}\n백업: {run}")
        try:
            for identity, client, pending, item in live:
                item['shutdown'] = shutdown(identity, client, pending)
                write_private_json(run/'manifest.json', manifest)
            for number, item in enumerate(projects):
                root = Path(item['data_root'])
                identity = inspect_backend(DataLayout(root))
                if identity.state != 'stopped':
                    raise DataDirError('WB root가 stopped 상태가 아닙니다.')
                lock = InstanceLock(identity.layout.lock)
                if identity.layout.lock.exists():
                    lock.acquire_existing_readonly()
                    stack.callback(lock.release)
                elif identity.record is not None:
                    raise DataDirError('WB writer lock 미확인')
                require_no_writers(root)
                item['backup'] = 'projects/'+str(number)
                item.update(update_backup.snapshot(root, run/item['backup']))
                require_no_writers(root)
                item['resume'] = shlex.join(['wb','start','--data-dir',str(root)])
            manifest['backup_complete'] = True
            write_private_json(run/'manifest.json', manifest)
            for item in projects:
                write_private_json(Path(item['data_root'])/'update-resume.json',
                    {'schema_version':1, 'backup':str(run), 'previous':install['previous'], 'target':target,
                     'resume':item['resume'], 'task':(item['snapshot'] or {}).get('task'),
                     'sessions':(item['snapshot'] or {}).get('bridge'), 'shutdown':item['shutdown'],
                     'state':{key:(item['snapshot'] or {}).get(key) for key in
                              ('holds','automation','worker','startup')}})
            manifest['installation'] = 'unknown'
            write_private_json(run/'manifest.json', manifest)
            result = command([install['uv'], 'tool', 'install', '--offline', '--force', '--python', install['python'],
                              'git+'+install['url']+'@'+target], env=dict(os.environ, UV_PYTHON_DOWNLOADS='never'),
                              pass_fds=(fence_descriptor,))
            manifest['uv_returncode'] = result.returncode
            if result.returncode == 0:
                verified = command([Path(install['prefix'])/'bin/python', '-I', '-c', VERIFY,
                                    json.dumps({'url':install['url'], 'commit':target, 'prefix':install['prefix']})])
                if verified.returncode == 0 and json.loads(verified.stdout).get('verified') is True:
                    manifest['installation'] = 'verified'
            manifest['complete'] = manifest['installation'] == 'verified'
            write_private_json(run/'manifest.json', manifest)
            if not manifest['complete']:
                raise DataDirError('새 설치 검증 실패')
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as error:
            reason = str(error) if isinstance(error, DataDirError) else type(error).__name__
            manifest['failure_reason'] = reason
            try:
                write_private_json(run/'manifest.json', manifest)
            except OSError:
                pass  # 실패 기록을 못 써도 backup·수동 복구 안내는 표시한다.
            print(f"업데이트 중단: {reason}; 설치 상태 {manifest['installation']} (uv exit {manifest.get('uv_returncode', '미실행/미확인')}). 백업 {run}\n"
                  f"이전/목표 commit: {install['previous']} / {target}\n"
                  f"이전 ref 수동 재설치: {manifest['manual_reinstall']}{hub_note}", file=sys.stderr)
            return 1
        print('업데이트 완료: 새 interpreter·출처 commit·필수 assets 확인')
        return 0
