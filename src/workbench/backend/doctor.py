"""수정·초기화 없이 로컬 상태를 제한된 근거로 진단한다."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time

from .install_recipe import inspect_existing
from .project import resolve_project_context
from .setup import SetupError, resolve_runtime_selection
from .migration_sqlite import committed_image
from .paths import DataDirError, DataLayout
from .omp_home import user_auth_store, auth_store_state
from .process_identity import inspect_backend, BackendIdentityError
from .client import UiClient
from .maintenance import FENCE_BASE


def maintenance(add):
    lock = FENCE_BASE/'update.lock'
    try:
        if os.path.lexists(lock):
            info = lock.lstat()
            parent = FENCE_BASE.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700):
                raise ValueError('unsafe update fence')
            with open('/proc/locks', 'rb') as stream:
                rows = stream.read((1 << 20)+1)
            if len(rows) > 1 << 20:
                raise ValueError('lock inventory limit')
            active = False
            for row in rows.splitlines():
                fields = row.split()
                if len(fields) < 6 or fields[1] != b'FLOCK':
                    continue
                device = fields[5].split(b':')
                if len(device) == 3 and (int(device[0],16),int(device[1],16),int(device[2])) == (os.major(info.st_dev),os.minor(info.st_dev),info.st_ino):
                    active = True
            current = lock.lstat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError('fence changed')
        else:
            active = False
        add('update', 'warning' if active else 'ok', 'update/start 유지보수 진행 중' if active else '활성 update/start fence 없음', '진행 중 유지보수를 자동 중단하지 않습니다.')
    except (OSError, ValueError):
        add('update', 'unknown', 'update fence 관측 불가', '동시 유지보수·fence 권한과 /proc/locks 접근을 확인하세요.')
    state = os.environ.get('XDG_STATE_HOME', '')
    base = (Path(state) if os.path.isabs(state) else Path.home()/'.local/state')/'omp-workbench/updates'
    try:
        if not os.path.lexists(base):
            add('backup', 'ok', '보관된 update backup 없음', '백업/복원은 doctor가 수행하지 않습니다.')
            return
        info = base.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('unsafe backup root')
        count = 0
        with os.scandir(base) as entries:
            for entry in entries:
                count += 1
                if count > 128 or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    raise ValueError('backup inventory unknown')
                run = Path(entry.path)
                info = run.lstat()
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError('unsafe backup run')
                manifest = json.loads(read_bytes(run/'manifest.json', 1 << 20))
                if manifest.get('schema_version') != 1:
                    raise ValueError('unknown manifest')
                if (manifest.get('complete') is not True or manifest.get('backup_complete') is not True
                        or manifest.get('installation') != 'verified'):
                    add('backup', 'error', '실패/부분 update·backup 기록 존재', 'manifest와 이전/목표 ref·백업을 직접 확인하세요. 자동 복구하지 않습니다.')
                    return
                files = 0
                for project in manifest['projects']:
                    relative = Path(project['backup'])
                    if relative.is_absolute() or '..' in relative.parts:
                        raise ValueError('invalid backup path')
                    for item in project['files']:
                        files += 1
                        relative_file = Path(item['path'])
                        if files > 4096 or relative_file.is_absolute() or '..' in relative_file.parts:
                            raise ValueError('backup file inventory limit')
                        copied = run/relative/relative_file
                        for parent in (copied, *copied.parents):
                            if parent == run:
                                break
                            if parent.is_symlink():
                                raise ValueError('symlink backup')
                        metadata = copied.lstat()
                        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size != item['bytes']):
                            raise ValueError('partial backup')
        add('backup', 'ok', f'완료 update manifest {count}개·backup 파일 metadata 확인', '파일 내용/hash를 재검증하지 않았습니다. 실제 복원은 별도 판단하세요.')
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        add('backup', 'unknown', 'backup/manifest 누락·불일치·확인 불가', '원본과 manifest를 보존해 부분 백업·권한·크기 제한을 확인하세요.')


def backend(layout, add):
    try:
        identity = inspect_backend(layout)
        if identity.state != 'running':
            add('backend', 'warning', 'backend '+identity.state, '시작 중이면 기다린 뒤 다시 확인하세요. doctor는 backend를 시작/종료하지 않습니다.')
            add('bridge', 'warning' if identity.state == 'stopped' else 'unknown', '실행 중 bridge snapshot 없음', '실행 중 backend에서 다시 확인하세요.')
            return
        with UiClient(identity.layout.ui_socket, name='workbench-doctor', timeout=3,
                      expected_process=identity.record['processes']['backend']) as client:
            snapshot = client.snapshot()
        observed = snapshot.get('backend', {})
        if (observed.get('pid') != identity.record['processes']['backend']['pid']
                or observed.get('data_dir') != str(layout.root)):
            raise ValueError('snapshot identity mismatch')
        add('backend', 'ok', '검증된 live backend snapshot', '조회만 수행했습니다. backend를 종료하거나 작업을 변경하지 않습니다.')
        bridge = snapshot.get('bridge', {})
        roles = [bridge.get(role, {}) for role in ('manager','worker')]
        if all(item.get('connected') is True and item.get('pid_matches_pane') is True and item.get('session_id') for item in roles):
            add('bridge', 'ok', 'manager/worker bridge 연결·process binding 확인', '기존 snapshot 관측이며 SDK/model 요청을 하지 않았습니다.')
        else:
            status = 'error' if any(item.get('connected') is False or item.get('pid_matches_pane') is False for item in roles) else 'unknown'
            add('bridge', status, 'bridge 연결/binding 누락 또는 미확인', 'backend·선택 OMP·bridge 상태를 직접 확인하세요.')
    except BackendIdentityError:
        add('backend', 'error', 'backend identity 불일치 또는 미확인', 'UID/boot/PID/start ticks·lock·endpoint binding을 확인하세요.')
        add('bridge', 'unknown', 'identity 미확인으로 bridge 조회 안 함', '검증되지 않은 endpoint에 연결하지 않습니다.')
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, TimeoutError):
        add('backend', 'unknown', 'backend endpoint/snapshot 확인 불가', '실행 상태와 endpoint를 확인하고 다시 진단하세요.')
        add('bridge', 'unknown', '검증된 bridge snapshot 없음', '프로세스를 자동 종료하거나 재시작하지 않습니다.')


def read_bytes(path, limit=64 << 20):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > limit):
            raise PermissionError('unsafe diagnostic source')
        value = bytearray()
        while chunk := os.read(descriptor, min(1 << 20, limit-len(value)+1)):
            value.extend(chunk)
            if len(value) > limit:
                raise ValueError('diagnostic source limit')
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(before) != identity(os.fstat(descriptor)) or identity(before) != identity(path.lstat()):
            raise RuntimeError('diagnostic source changed')
        return bytes(value)
    finally:
        os.close(descriptor)


def database(path, expected_version, required_tables):
    try:
        family = [path, Path(str(path)+'-wal'), Path(str(path)+'-journal')]
        before = [(item.lstat().st_ino, item.stat().st_size, item.stat().st_mtime_ns) if os.path.lexists(item) else None for item in family]
        if before[0] is None:
            return 'warning', 'DB 없음/미생성', '필요한 프로젝트/store인지 확인하세요. 자동 생성하지 않습니다.'
        main = read_bytes(path)
        wal = read_bytes(family[1]) if before[1] else b''
        if before[2] and read_bytes(family[2]):
            return 'unknown', 'SQLite rollback journal 존재', '원래 writer 상태를 확인한 뒤 다시 진단하세요.'
        after = [(item.lstat().st_ino, item.stat().st_size, item.stat().st_mtime_ns) if os.path.lexists(item) else None for item in family]
        if before != after:
            return 'unknown', 'DB가 진단 중 변경됨', 'writer의 일관된 상태에서 다시 진단하세요.'
        image, _ = committed_image(main, wal)
        connection = sqlite3.connect(':memory:')
        try:
            connection.deserialize(image)
            deadline = time.monotonic()+2
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA query_only=ON')
            if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                return 'error', 'SQLite 무결성 오류', '원본과 백업을 보존하고 손상 원인을 확인하세요.'
            schema = connection.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if schema != expected_version or not required_tables <= tables:
                return 'error', '지원 schema/table 불일치', '원본 보존 후 설치 버전·명시적 이관 절차를 확인하세요.'
        finally:
            connection.close()
        return 'ok', f'SQLite memory snapshot 무결성·schema {schema} 확인', '원본을 SQLite로 열지 않았으며 live 상태의 이후 변경은 보장하지 않습니다.'
    except PermissionError:
        return 'error', 'DB 권한/종류/소유자 또는 크기 오류', 'private 파일과 진단 크기 제한(64MiB)을 확인하세요.'
    except (sqlite3.DatabaseError, ValueError, DataDirError):
        return 'error', 'SQLite 손상 또는 형식 오류', '원본과 백업을 보존하고 확인하세요.'
    except (OSError, RuntimeError):
        return 'unknown', 'DB snapshot 확인 불가', 'writer·권한·DB family 상태를 확인하세요.'


def version(path, pattern):
    """외부 version stdout만 allowlist로 파싱; stderr/오류 원문은 버린다."""
    child = None
    try:
        child = subprocess.Popen([str(path), '--version'], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        output, total, deadline = bytearray(), 0, time.monotonic()+3
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, True)
            selector.register(child.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    return None
                for key, _ in selector.select(max(0, deadline-time.monotonic())):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > 4096:
                        return None
                    if key.data:
                        output.extend(chunk)
        while (status := os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)) is None:
            if time.monotonic() >= deadline:
                return None
            time.sleep(.01)
        match = re.fullmatch(pattern, output.decode('utf-8', errors='replace').strip())
        return match.group(1) if status.si_code == os.CLD_EXITED and status.si_status == 0 and match else None
    except OSError:
        return None
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


def installation():
    distribution = importlib.metadata.distribution('workbench')
    release = distribution.version
    if not isinstance(release, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?(?:\.(?:post|dev)[0-9]+)?(?:\+[0-9a-f]{7,40})?', release):
        return 'unknown', 'WB version metadata 형식 미확인', '실행 distribution metadata를 확인하세요.'
    source = json.loads(distribution.read_text('direct_url.json') or '{}')
    entry = Path(sys.argv[0]).resolve(strict=True)
    prefix = Path(sys.prefix).resolve()
    entry_matches = (entry.is_relative_to(prefix/'bin')
                     or entry == Path(distribution.locate_file('workbench/__main__.py')).resolve(strict=True))
    if (not Path(distribution.locate_file('')).resolve().is_relative_to(prefix)
            or Path(sys.executable) != Path(sys.prefix)/'bin/python' or not entry_matches):
        return 'unknown', '실행 distribution·Python·entrypoint 결속 미확인', '실제로 실행한 설치 경로와 Python을 확인하세요.'
    vcs = source.get('vcs_info', {})
    if vcs.get('vcs') == 'git':
        commit = vcs.get('commit_id', '')
        if not re.fullmatch('[0-9a-f]{40,64}', commit):
            return 'unknown', 'Git commit 미확인', '설치 출처를 확인하세요.'
        detail = 'Git commit '+commit
    else:
        detail = 'editable' if source.get('dir_info', {}).get('editable') else 'wheel/일반 distribution'
    installer = (distribution.read_text('INSTALLER') or '').strip()
    method = installer if installer in ('uv', 'pip') else '확인 불가'
    return ('warning' if source.get('dir_info', {}).get('editable') else 'ok'), f'WB {release} · {detail} · installer {method} · Python/entrypoint 일치', '업데이트 지원 조건은 wb update 안내를 확인하세요.'


def diagnose(args):
    checks = []
    def add(identifier, status, summary, hint):
        checks.append({'id':identifier, 'status':status, 'summary':summary, 'hint':hint})
    try:
        add('installation', *installation())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, importlib.metadata.PackageNotFoundError):
        add('installation', 'unknown', '실행 설치 metadata 확인 불가', '실제 설치/entrypoint를 확인하세요.')
    add('python', 'ok' if sys.version_info >= (3, 12) else 'error',
        'Python '+'.'.join(map(str, sys.version_info[:3])), 'Python 3.12 이상을 사용하세요.')
    uv = shutil.which('uv')
    observed = version(uv, r'uv ([0-9]+\.[0-9]+\.[0-9]+)(?: \([^\r\n)]{1,120}\))?') if uv else None
    add('uv', 'ok' if observed else 'error', 'uv '+observed if observed else 'uv 누락 또는 version 확인 실패', 'uv 설치/실행 경로를 직접 확인하세요.')
    try:
        selected = resolve_runtime_selection(environment=os.environ)
        omp = inspect_existing(selected.path, timeout=3, environment=os.environ)
        add('omp', 'ok' if omp.status == 'executable' else 'error',
            'OMP '+omp.version if omp.status == 'executable' else '선택 OMP 누락 또는 version 확인 실패',
            '선택 OMP 경로를 확인하세요. doctor는 로그인·SDK/provider 검사를 하지 않습니다.')
    except SetupError as error:
        # The selection itself is refused (plain wb cannot start): say why and how, not just "unknown".
        add('omp', 'error', f'OMP 선택 실패: {error.code}' + (f' — {error.detail}' if error.detail else ''),
            (f'`{error.fix}` 실행 후 ' if error.fix else '원인을 해결한 뒤 ') +
            '`wb setup`을 다시 실행하세요. 임시로는 WORKBENCH_OMP=<omp 경로> 로 실행할 수 있습니다.')
    except (OSError, ValueError, RuntimeError):
        add('omp', 'unknown', '선택 OMP metadata 확인 불가', 'WORKBENCH_OMP 또는 사용자 OMP 선택을 확인하세요.')
    try:
        auth = user_auth_store(os.environ)
        observed = auth_store_state(auth)
        if observed.detail == 'missing':
            add('auth', 'warning', '공유 auth store 없음', '로그인/provider 상태는 별도로 확인하세요. doctor는 인증을 검사하지 않습니다.')
        elif observed.ok and stat.S_IMODE(auth.lstat().st_mode) == 0o600:
            add('auth', 'ok', 'auth store 존재·private metadata 확인', '내용을 읽지 않았으며 로그인/token 유효성은 확인하지 않습니다.')
        else:
            add('auth', 'error', 'auth store 소유자/권한/종류 오류', '사용자 소유 regular file·0600 상태를 직접 확인하세요.')
    except (OSError, ValueError, RuntimeError):
        add('auth', 'unknown', 'auth store metadata 확인 불가', '선택된 사용자 store 경로와 권한을 확인하세요.')
    maintenance(add)
    try:
        context = resolve_project_context(Path.cwd(), os.environ, args.data_dir)
        add('project', 'warning' if context.needs_initialization else 'ok',
            '현재 프로젝트 없음' if context.needs_initialization else '프로젝트 context 확인',
            '프로젝트 안에서 또는 --data-dir로 다시 확인하세요.' if context.needs_initialization else '초기화/이관은 별도 명령입니다.')
        if not context.needs_initialization:
            try:
                info = context.data_root.lstat()
                private = stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
            except OSError:
                private = False
            add('permissions', 'ok' if private else 'error', 'WB root private 권한 확인' if private else 'WB root 없음 또는 private 권한 오류', '소유자·0700 root를 직접 확인하세요. doctor는 권한을 바꾸지 않습니다.')
            if private:
                add('db.tasks', *database(context.data_root/'tasks.sqlite3', 3, {'tasks','task_specs','runs','task_decisions'}))
                add('db.records', *database(context.data_root/'records.sqlite3', 1, {'db_meta','records','revisions'}))
                backend(DataLayout(context.data_root, runtime_root=context.runtime_root), add)
                resume = context.data_root/'update-resume.json'
                if os.path.lexists(resume):
                    try:
                        marker = json.loads(read_bytes(resume, 1 << 20))
                        if marker.get('schema_version') != 1:
                            raise ValueError('unknown resume metadata')
                        add('resume', 'warning', '이전 update의 명시적 재개 안내 존재', '원래 프로젝트의 wb start 안내와 백업을 확인하세요. 자동 replay/재개하지 않습니다.')
                    except (OSError, ValueError, TypeError, AttributeError):
                        add('resume', 'unknown', 'update 재개 metadata 확인 불가', '원본 marker/백업을 보존한 뒤 확인하세요.')
                else:
                    add('resume', 'ok', 'update 재개 marker 없음', 'doctor는 marker를 만들지 않습니다.')
    except (OSError, ValueError, RuntimeError):
        add('project', 'error', '프로젝트 context 오류', '프로젝트 marker와 명시 data-dir를 확인하세요.')
    overall = 'error' if any(item['status'] in ('error', 'unknown') for item in checks) else 'warning' if any(item['status'] == 'warning' for item in checks) else 'ok'
    return {'schema_version':1, 'overall':overall, 'checks':checks}


def run(args):
    report = diagnose(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print('WB doctor: '+report['overall'])
        for item in report['checks']:
            print(f"[{item['status']}] {item['id']}: {item['summary']}\n  {item['hint']}")
    return 1 if report['overall'] == 'error' else 0
