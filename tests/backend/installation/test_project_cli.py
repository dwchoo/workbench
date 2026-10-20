"""Public CLI behavior with disposable projects; real backend coverage is live_project_sessions.py."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]


class ProjectCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wi-cli-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'research'
        self.project.mkdir()
        home = self.base / 'home'
        home.mkdir(mode=0o700)
        runtime = self.base / 'run'
        runtime.mkdir(mode=0o700)
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'XDG_RUNTIME_DIR': str(runtime)}

    def cli(self, *args, cwd=None, env=None):
        return subprocess.run([sys.executable, '-m', 'workbench', *args],
                              cwd=cwd or self.project, env=env or self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def delayed_frontend(self, tag, env, *, plain=False, stdin=None, stdout=None):
        """Only OS exec boundary substitutes an owned contract server for OMP/backend.

        Public CLI still admits/forks/gates/waits/attaches; expected old/new record
        bytes and actual lock interval are independently controlled by files.
        """
        fixture = self.base / 'delayed-backend.py'
        fixture.write_text(r'''import fcntl,json,os,socket,sys,threading,time
from pathlib import Path
sys.path.insert(0, SOURCE)
from workbench.contracts.ui_v1 import FrameDecoder,encode_frame
args=sys.argv
def value(name):return args[args.index(name)+1]
data=Path(value('--data-dir'));runtime=Path(value('--runtime-root'));project=value('--project-dir')
lock=os.open(data/'backend.lock',os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
previous=data/'backend.json'
Path(os.environ['WI_FIXTURE_PREVIOUS']).write_bytes(previous.read_bytes() if previous.exists() else b'')
ticks=int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
Path(os.environ['WI_FIXTURE_READY']).write_text(json.dumps({'pid':os.getpid(),'start_ticks':ticks}))
deadline=time.monotonic()+8
while not Path(os.environ['WI_FIXTURE_GATE']).exists():
    if time.monotonic()>deadline:raise SystemExit(7)
    time.sleep(.01)
marker=json.loads((data/'project.json').read_text());runtime.mkdir(parents=True,mode=0o700,exist_ok=True)
binding=runtime/'runtime.json';binding.write_text(json.dumps({'schema_version':1,'data_root':str(data),'project_id':marker['project_id']}));binding.chmod(0o600)
ref={'pid':os.getpid(),'start_ticks':ticks}
record={'data_dir':str(data),'project_dir':project,'runtime_root':str(runtime),'project_id':marker['project_id'],'uid':os.geteuid(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'ui_socket':str(runtime/'ui.sock'),'bridge_socket':str(runtime/'bridge.sock'),'processes':{'backend':ref}}
snapshot={'phase':'ready','reason':None,'backend':dict(record,pid=os.getpid(),process=ref),'panes':{},'omp_isolation':{'state':'ok'},'attached':False}
with socket.socket(socket.AF_UNIX) as server:
    server.bind(record['ui_socket']);server.listen()
    temporary=data/'fixture-record';temporary.write_text(json.dumps(record));temporary.chmod(0o600);temporary.replace(data/'backend.json')
    def serve(connection):
        with connection:
            decoder=FrameDecoder()
            while data_in:=connection.recv(65536):
                for frame in decoder.feed(data_in):
                    kind=frame.header['type']
                    if kind=='hello':reply={'v':1,'type':'welcome','version':1}
                    else:
                        reply={'v':1,'type':'result','id':frame.header['id'],'ok':True}
                        if kind in ('snapshot','attach'):reply['snapshot']=snapshot
                        if kind=='attach':
                            snapshot['attached']=True
                            with Path(os.environ['WI_FIXTURE_ATTACH']).open('a') as stream:stream.write(json.dumps(record)+'\n')
                    connection.sendall(encode_frame(reply))
    while True:
        connection,_=server.accept();threading.Thread(target=serve,args=(connection,),daemon=True).start()
'''.replace('SOURCE', repr(str(REPO / 'src'))))
        code = """import os,sys,json,errno,time
from pathlib import Path
from unittest.mock import patch
from workbench.backend.cli import main
actual=os.execv
actual_fork=os.fork
actual_link=os.link
fork_depth=0
fixture=sys.argv[1]
def tracked_fork():
    global fork_depth
    fork_depth+=1
    result=actual_fork()
    if result==0:
        ticks=int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ',1)[1].split()[19])
        with Path(os.environ['WI_FIXTURE_FORKS']).open('a') as stream:
            stream.write(json.dumps({'pid':os.getpid(),'start_ticks':ticks,'depth':fork_depth})+'\\n')
    return result
def controlled_link(source,target,*args,**kwargs):
    if target=='backend-starting.json':
        if os.environ.get('WI_FIXTURE_SWAP_ROOT'):
            root=Path(os.environ['WI_FIXTURE_SWAP_ROOT'])
            root.rename(root.with_name('.wb-moved'))
            root.symlink_to(os.environ['WI_FIXTURE_OTHER_ROOT'],target_is_directory=True)
        if os.environ.get('WI_FIXTURE_PUBLISH_FAIL'):
            raise OSError(errno.ENOSPC,'controlled admission publication failure')
        if os.environ.get('WI_FIXTURE_PUBLISH_WAIT'):
            Path(os.environ['WI_FIXTURE_PUBLISH_WAIT']).touch()
            time.sleep(8)
    return actual_link(source,target,*args,**kwargs)
def external_backend(path,argv):
    actual(sys.executable,[sys.executable,'-I',fixture,*argv])
with patch('os.execv',external_backend), patch('os.fork',tracked_fork), patch('os.link',controlled_link):
    raise SystemExit(main(['start','--timeout','4',*sys.argv[2:]]))
"""
        out, err = self.base / (tag + '.out'), self.base / (tag + '.err')
        with out.open('wb') as out_file, err.open('wb') as err_file:
            child = subprocess.Popen([sys.executable, '-c', code, str(fixture), '--plain' if plain else '--no-attach'],
                                     env=env, cwd=self.project, stdin=stdin or subprocess.DEVNULL,
                                     stdout=stdout if stdout is not None else out_file,
                                     stderr=stdout if stdout is not None else err_file, start_new_session=True)
        self.addCleanup(self.stop_frontend_fixture, child)
        return child, out, err

    @staticmethod
    def stop_frontend_fixture(child):
        import signal
        try:
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=3)

    def fixture_context(self, *, previous_boot=False):
        import signal
        from workbench.backend.project import initialize_project, resolve_project_context
        context = initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                                     interactive=True, environment=self.env)
        # Observe a real prior PID/ticks, then end it normally, so the old record is
        # meaningful history instead of invented dead PID evidence.
        old = subprocess.Popen([sys.executable, '-c', 'input()'], stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ticks = int(Path(f'/proc/{old.pid}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])
        old.stdin.write(b'\n');old.stdin.close();old.wait(timeout=3)
        record = {'data_dir': str(context.data_root), 'project_dir': str(self.project),
                  'project_id': context.project_id, 'runtime_root': str(context.runtime_root),
                  'ui_socket': str(context.runtime_root / 'ui.sock'), 'bridge_socket': str(context.runtime_root / 'bridge.sock'),
                  'uid': os.geteuid(), 'boot_id': 'prior-fixture-boot' if previous_boot else Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  'processes': {'backend': {'pid': old.pid, 'start_ticks': ticks}}}
        path = context.data_root / 'backend.json';path.write_text(json.dumps(record));path.chmod(0o600)
        omp = self.base / 'fixture-omp';omp.write_text('#!/bin/sh\necho omp/18.12.2\n');omp.chmod(0o700)
        env = dict(self.env, WORKBENCH_OMP=str(omp), OPENAI_API_KEY='fixture-only',
                   WI_FIXTURE_READY=str(self.base / 'new-child-ready'), WI_FIXTURE_GATE=str(self.base / 'release-publish'),
                   WI_FIXTURE_PREVIOUS=str(self.base / 'previous-record'), WI_FIXTURE_ATTACH=str(self.base / 'attachments'),
                   WI_FIXTURE_FORKS=str(self.base / 'fork-identities'))
        def cleanup():
            ready = Path(env['WI_FIXTURE_READY'])
            references = ([json.loads(ready.read_text())] if ready.exists() else [])
            forks = Path(env['WI_FIXTURE_FORKS'])
            if forks.exists():
                references += [json.loads(line) for line in forks.read_text().splitlines()]
            for ref in references:
                stop_reference(ref)
        def stop_reference(ref):
            try:
                fd = os.pidfd_open(ref['pid'])
            except ProcessLookupError:return
            try:
                observed = Path(f'/proc/{ref["pid"]}/stat').read_bytes().rsplit(b') ', 1)[1].split()
                if int(observed[19]) == ref['start_ticks']:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
            except FileNotFoundError:pass
            finally:os.close(fd)
        self.addCleanup(cleanup)
        return context, env, path.read_bytes()

    def wait_fixture_file(self, path, timeout=3):
        import time
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(path.exists(), str(path))

    def test_normal_restart_waits_for_admitted_child_to_replace_the_ended_record(self):
        import time
        context, env, previous = self.fixture_context()
        child, out, err = self.delayed_frontend('restart', env)
        self.wait_fixture_file(Path(env['WI_FIXTURE_READY']))
        self.assertEqual((context.data_root / 'backend.json').read_bytes(), previous)
        time.sleep(.25)  # The owned child holds the real lock across multiple CLI polls.
        Path(env['WI_FIXTURE_GATE']).touch()
        self.assertEqual(child.wait(timeout=5), 0, err.read_text())
        self.assertEqual(Path(env['WI_FIXTURE_PREVIOUS']).read_bytes(), previous)
        record = json.loads((context.data_root / 'backend.json').read_text())
        self.assertEqual(record['processes']['backend'], json.loads(Path(env['WI_FIXTURE_READY']).read_text()))

    def test_concurrent_start_plain_attach_uses_the_verified_runtime_with_its_process(self):
        import select
        import time
        context, env, previous = self.fixture_context()
        runtime_B = self.base / 'different-runtime'
        runtime_B.mkdir(mode=0o700)
        first, out, err = self.delayed_frontend('first', env)
        self.wait_fixture_file(Path(env['WI_FIXTURE_READY']))
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        try:
            second, _, _ = self.delayed_frontend('second', dict(env, XDG_RUNTIME_DIR=str(runtime_B)),
                                                  plain=True, stdin=slave, stdout=slave)
        finally:
            os.close(slave)
        time.sleep(.25)
        Path(env['WI_FIXTURE_GATE']).touch()
        self.assertEqual(first.wait(timeout=5), 0, err.read_text())
        output = bytearray()
        deadline = time.monotonic() + 4
        while not Path(env['WI_FIXTURE_ATTACH']).exists() and time.monotonic() < deadline:
            if select.select([master], [], [], .05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        self.assertTrue(Path(env['WI_FIXTURE_ATTACH']).exists(), output.decode(errors='replace'))
        attached = json.loads(Path(env['WI_FIXTURE_ATTACH']).read_text().splitlines()[0])
        self.assertEqual(attached['runtime_root'], str(context.runtime_root))
        self.assertEqual(attached['processes']['backend'], json.loads(Path(env['WI_FIXTURE_READY']).read_text()))
        import termios
        deadline = time.monotonic() + 2
        while termios.tcgetattr(master)[3] & termios.ICANON and time.monotonic() < deadline:
            if select.select([master], [], [], .02)[0]:
                output.extend(os.read(master, 65536))
        self.assertFalse(termios.tcgetattr(master)[3] & termios.ICANON)
        os.write(master, b'\x1dd')
        self.assertEqual(second.wait(timeout=3), 0, output.decode(errors='replace'))
        self.assertEqual(list(runtime_B.iterdir()), [])

    def test_previous_boot_history_waits_for_admitted_start_without_deleting_old_record(self):
        import time
        context, env, previous = self.fixture_context(previous_boot=True)
        child, out, err = self.delayed_frontend('prior-boot', env)
        self.wait_fixture_file(Path(env['WI_FIXTURE_READY']))
        self.assertEqual((context.data_root / 'backend.json').read_bytes(), previous)
        time.sleep(.25)
        Path(env['WI_FIXTURE_GATE']).touch()
        self.assertEqual(child.wait(timeout=5), 0, err.read_text())
        self.assertEqual(Path(env['WI_FIXTURE_PREVIOUS']).read_bytes(), previous)

    def test_ended_record_and_unrelated_held_lock_without_admission_remain_a_hold(self):
        from workbench.backend.paths import InstanceLock
        context, env, previous = self.fixture_context()
        lock = InstanceLock(context.data_root / 'backend.lock')
        lock.acquire()
        self.addCleanup(lock.release)
        for boot in (Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'prior-fixture-boot'):
            record = json.loads(previous)
            record['boot_id'] = boot
            content = json.dumps(record).encode()
            (context.data_root / 'backend.json').write_bytes(content)
            result = self.cli('start', '--no-attach', env=env)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('identity', result.stderr)
            self.assertFalse((context.data_root / 'backend-starting.json').exists())
            self.assertEqual((context.data_root / 'backend.json').read_bytes(), content)

    def test_publication_failure_or_parent_exit_does_not_leave_a_gated_child(self):
        import signal
        import time
        context, env, previous = self.fixture_context()
        for mode in ('failure', 'parent-exit'):
            with self.subTest(mode=mode):
                forks = Path(env['WI_FIXTURE_FORKS'])
                forks.unlink(missing_ok=True)
                extra = ({'WI_FIXTURE_PUBLISH_FAIL': '1'} if mode == 'failure' else
                         {'WI_FIXTURE_PUBLISH_WAIT': str(self.base / 'publisher-blocked')})
                child, out, err = self.delayed_frontend('gate-' + mode, dict(env, **extra))
                if mode == 'parent-exit':
                    self.wait_fixture_file(Path(extra['WI_FIXTURE_PUBLISH_WAIT']))
                    os.kill(child.pid, signal.SIGTERM)  # direct unreaped child PID is still owned
                status = child.wait(timeout=4)
                self.assertNotEqual(status, 0)
                self.wait_fixture_file(forks)
                refs = [item for item in map(json.loads, forks.read_text().splitlines()) if item['depth'] == 2]
                self.assertEqual(len(refs), 1)
                ref = refs[0]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        fields = Path(f'/proc/{ref["pid"]}/stat').read_bytes().rsplit(b') ', 1)[1].split()
                    except FileNotFoundError:
                        break
                    if int(fields[19]) != ref['start_ticks'] or fields[0] in (b'Z', b'X'):
                        break
                    time.sleep(.02)
                else:
                    self.fail('gated child survived publication failure/parent exit')
                self.assertEqual((context.data_root / 'backend.json').read_bytes(), previous)
                self.assertFalse((context.data_root / 'backend-starting.json').exists())
                self.assertFalse(Path(env['WI_FIXTURE_READY']).exists())

    def test_admission_publication_stays_on_its_validated_directory_after_root_swap(self):
        import time
        context, env, previous = self.fixture_context()
        other = self.base / 'other-private-state'
        other.mkdir(mode=0o700)
        target = other / 'backend-starting.json'
        content = b'{"do-not-overwrite":"existing unrelated admission"}\n'
        target.write_bytes(content)
        target.chmod(0o600)
        child, out, err = self.delayed_frontend('root-swap', dict(env,
            WI_FIXTURE_SWAP_ROOT=str(context.data_root), WI_FIXTURE_OTHER_ROOT=str(other)))
        self.assertEqual(child.wait(timeout=4), 1, err.read_text())
        self.assertIn('startup_root_changed', err.read_text())
        self.assertEqual(target.read_bytes(), content)
        moved = context.data_root.with_name('.wb-moved')
        self.assertEqual((moved / 'backend.json').read_bytes(), previous)
        self.assertTrue((moved / 'backend-starting.json').is_file())
        self.assertFalse(Path(env['WI_FIXTURE_READY']).exists())
        # Gate EOF is a real process boundary, not an assertion on private syntax.
        refs = [item for item in map(json.loads, Path(env['WI_FIXTURE_FORKS']).read_text().splitlines())
                if item['depth'] == 2]
        self.assertEqual(len(refs), 1)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                fields = Path(f'/proc/{refs[0]["pid"]}/stat').read_bytes().rsplit(b') ', 1)[1].split()
            except FileNotFoundError:
                break
            if fields[0] in (b'Z', b'X'):
                break
            time.sleep(.02)
        else:
            self.fail('root-swap failed admission left a gated child')

    def test_status_of_existing_legacy_directory_without_admission_keeps_read_only_behavior(self):
        data = self.base / 'legacy-readable'
        data.mkdir(mode=0o755)
        result = self.cli('status', '--data-dir', str(data), '--json')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'running': False, 'data_dir': str(data)})
        self.assertEqual(list(data.iterdir()), [])
        self.assertEqual(data.stat().st_mode & 0o777, 0o755)

    def test_status_missing_project_does_not_initialize_or_use_global_state(self):
        result = self.cli('status', '--json')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'running': False, 'data_dir': str(self.project / '.wb')})
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertFalse((self.base / 'home/.local/state/omp-workbench').exists())

    def test_bare_execution_has_start_requirement_guidance_without_any_creation(self):
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('wb setup', result.stderr)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(list((self.base / 'run').iterdir()), [])

    def test_explicit_compatibility_check_cannot_initialize_an_unconfirmed_project(self):
        # The existing verifier's external child boundary is blocked: this test only
        # observes project admission, never launches its runtime verification fixture.
        code = """from unittest.mock import patch
from workbench.backend.cli import main
with patch('subprocess.Popen', side_effect=OSError('fixture forbids external verification child')):
    raise SystemExit(main(['logs','summary-compat','verify','--scripted','--json']))
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=self.project, env=self.env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(list(self.project.iterdir()), [], result.stderr + result.stdout)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('initialize', result.stderr)

    def test_noninteractive_first_start_refuses_before_any_backend_creation(self):
        omp = self.base / 'omp'
        omp.write_text('#!/bin/sh\necho omp/18.12.2\n')
        omp.chmod(0o700)
        env = dict(self.env, WORKBENCH_OMP=str(omp), OPENAI_API_KEY='loopback-fixture-only')
        # External fork is stopped at the OS seam so RED cannot leave an orphan backend.
        code = """from unittest.mock import patch
from workbench.backend.cli import main
with patch('os.fork', side_effect=RuntimeError('backend creation attempted before confirmation')):
    raise SystemExit(main(['start','--no-attach']))
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=self.project, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('confirmation', result.stderr)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(list((self.base / 'run').iterdir()), [])

    def test_mismatched_durable_backend_identity_is_held_without_attach_or_mutation(self):
        legacy = self.base / 'legacy'
        legacy.mkdir(mode=0o700)
        ticks = int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])
        record = {'data_dir': str(legacy), 'project_dir': str(self.project),
                  'ui_socket': str(legacy / 'ui.sock'), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  'uid': os.geteuid(), 'processes': {'backend': {'pid': os.getpid(), 'start_ticks': ticks}}}
        for changed in ({'data_dir': '/unexpected/data'}, {'project_dir': '/absent-project-root'},
                        {'boot_id': None}, {'uid': os.geteuid() + 1},
                        {'processes': {'backend': {'pid': os.getpid(), 'start_ticks': ticks + 1}}}):
            with self.subTest(changed=changed):
                content = json.dumps(dict(record, **changed)).encode()
                marker = legacy / 'backend.json'
                marker.write_bytes(content)
                marker.chmod(0o600)
                result = self.cli('status', '--data-dir', str(legacy), '--json')
                self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
                self.assertIn('identity', result.stderr)
                self.assertEqual(marker.read_bytes(), content)

    def test_missing_runtime_for_a_live_record_is_an_identity_hold_without_traceback(self):
        from workbench.backend.boot import read_boot_id
        from workbench.backend.paths import InstanceLock
        from workbench.backend.project import initialize_project, resolve_project_context
        context = initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                                     interactive=True, environment=self.env)
        ticks = int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])
        record = {'data_dir': str(context.data_root), 'project_dir': str(self.project),
                  'project_id': context.project_id, 'runtime_root': str(context.runtime_root),
                  'ui_socket': str(context.runtime_root / 'ui.sock'),
                  'bridge_socket': str(context.runtime_root / 'bridge.sock'), 'uid': os.geteuid(),
                  'boot_id': read_boot_id(), 'processes': {'backend': {'pid': os.getpid(), 'start_ticks': ticks}}}
        path = context.data_root / 'backend.json'
        content = json.dumps(record).encode()
        path.write_bytes(content)
        path.chmod(0o600)
        lock = InstanceLock(context.data_root / 'backend.lock')
        lock.acquire()
        self.addCleanup(lock.release)
        # initialize_project creates no runtime; a live record is not sufficient proof
        # to recreate or select another endpoint when its namespace is absent.
        self.assertFalse(context.runtime_root.exists())
        result = self.cli('status', '--json')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('identity', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(path.read_bytes(), content)
        self.assertFalse(context.runtime_root.exists())

    def test_marked_backend_uuid_project_and_endpoints_must_match_durable_context(self):
        from workbench.backend.project import initialize_project, resolve_project_context
        context = initialize_project(resolve_project_context(self.project, self.env), confirm=lambda _: True,
                                     interactive=True, environment=self.env)
        alternate = self.base / 'another-project'
        alternate.mkdir()
        ticks = int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])
        record = {'data_dir': str(context.data_root), 'project_dir': str(self.project),
                  'project_id': context.project_id, 'runtime_root': str(context.runtime_root),
                  'ui_socket': str(context.runtime_root / 'ui.sock'),
                  'bridge_socket': str(context.runtime_root / 'bridge.sock'),
                  'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  'uid': os.geteuid(), 'processes': {'backend': {'pid': os.getpid(), 'start_ticks': ticks}}}
        for changed in ({'project_id': '7098ffdc-a1f0-43b8-be27-3080c4d930c2'},
                        {'project_dir': str(alternate)}, {'ui_socket': str(self.base / 'elsewhere.sock')}):
            with self.subTest(changed=changed):
                marker = context.data_root / 'backend.json'
                content = json.dumps(dict(record, **changed)).encode()
                marker.write_bytes(content)
                marker.chmod(0o600)
                result = self.cli('status', '--json')
                self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
                self.assertIn('identity', result.stderr)
                self.assertEqual(marker.read_bytes(), content)

    def test_unrecorded_socket_is_never_contacted_as_a_backend(self):
        legacy = self.base / 'legacy'
        legacy.mkdir(mode=0o700)
        called = self.base / 'unexpected-connection'
        ready = self.base / 'ready'
        code = """import socket,sys
from pathlib import Path
with socket.socket(socket.AF_UNIX) as server:
    server.bind(sys.argv[1]); server.listen(); server.settimeout(2)
    Path(sys.argv[2]).touch()
    try:
        connection,_=server.accept()
    except TimeoutError:
        pass
    else:
        Path(sys.argv[3]).write_text('unverified endpoint contacted')
        connection.close()
"""
        child = subprocess.Popen([sys.executable, '-c', code, str(legacy / 'ui.sock'), str(ready), str(called)],
                                 env=self.env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import time
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            result = self.cli('status', '--data-dir', str(legacy), '--json')
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse(called.exists(), 'status contacted an endpoint without durable backend identity')
        finally:
            child.wait(timeout=4)

    def test_peer_credential_uid_is_independent_of_primary_gid(self):
        import socket
        import struct
        import time
        from unittest.mock import patch
        from workbench.backend.client import ClientError, UiClient
        original_socket = socket.socket
        for name, uid, gid, expected_uid, allowed in (
                ('same-uid', os.geteuid(), os.geteuid() + 17, os.geteuid(), True),
                ('foreign-uid', os.geteuid() + 191, os.geteuid(), os.geteuid(), False),
                ('unsigned-uid', 3000000001, 3000000019, 3000000001, True)):
            with self.subTest(name=name):
                endpoint, ready, received = (self.base / (name + suffix) for suffix in ('.sock', '.ready', '.received'))
                code = """import socket,sys
from pathlib import Path
from workbench.contracts.ui_v1 import encode_frame
with socket.socket(socket.AF_UNIX) as server:
    server.bind(sys.argv[1]);server.listen();server.settimeout(3);Path(sys.argv[2]).touch()
    connection,_=server.accept()
    with connection:
        value=connection.recv(4096);Path(sys.argv[3]).write_bytes(value)
        if value:connection.sendall(encode_frame({'v':1,'type':'welcome','version':1}))
"""
                child = subprocess.Popen([sys.executable, '-c', code, str(endpoint), str(ready), str(received)],
                                         env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                class CredentialsOnly:
                    def __init__(self, *args, **kwargs):
                        self.real = original_socket(*args, **kwargs)
                    def __getattr__(self, key):
                        return getattr(self.real, key)
                    def getsockopt(self, level, option, *args):
                        if level == socket.SOL_SOCKET and option == socket.SO_PEERCRED:
                            return struct.pack('iII', child.pid, uid, gid)
                        return self.real.getsockopt(level, option, *args)
                try:
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists())
                    with patch('socket.socket', CredentialsOnly), patch('workbench.backend.client.os.geteuid', return_value=expected_uid):
                        if allowed:
                            with UiClient(endpoint) as client:
                                self.assertEqual(client.version, 1)
                        else:
                            with self.assertRaisesRegex(ClientError, 'UID mismatch'):
                                UiClient(endpoint)
                finally:
                    child.wait(timeout=4)
                self.assertEqual(bool(received.read_bytes()), allowed)

    def test_ui_peer_pid_mismatch_is_refused_before_sending_protocol_data(self):
        from workbench.backend.client import ClientError, UiClient
        import time
        endpoint = self.base / 'peer.sock'
        ready, received = self.base / 'peer-ready', self.base / 'peer-received'
        code = """import socket,sys
from pathlib import Path
with socket.socket(socket.AF_UNIX) as server:
    server.bind(sys.argv[1]); server.listen(); server.settimeout(2)
    Path(sys.argv[2]).touch()
    try:
        connection,_=server.accept()
    except TimeoutError:
        pass
    else:
        with connection:
            Path(sys.argv[3]).write_bytes(connection.recv(4096))
"""
        child = subprocess.Popen([sys.executable, '-c', code, str(endpoint), str(ready), str(received)],
                                 env=self.env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            ticks = int(Path(f'/proc/{os.getpid()}/stat').read_bytes().rsplit(b') ', 1)[1].split()[19])
            with self.assertRaisesRegex(ClientError, 'identity'):
                UiClient(endpoint, expected_process={'pid': os.getpid(), 'start_ticks': ticks})
        finally:
            child.wait(timeout=4)
        self.assertEqual(received.read_bytes(), b'')

    def test_saved_setup_selection_is_used_without_installing_or_changing_it(self):
        candidate = self.base / 'saved-omp'
        probed = self.base / 'version-probed'
        candidate.write_text(f'#!/bin/sh\necho omp/18.12.2\n: > {probed}\n')
        candidate.chmod(0o700)
        config = self.base / 'config/omp-workbench'
        config.mkdir(parents=True, mode=0o700)
        config.parent.chmod(0o700)
        selection = config / 'setup.json'
        original = json.dumps({'schema_version': 1, 'state': 'ready', 'path': str(candidate)}).encode()
        selection.write_bytes(original)
        selection.chmod(0o600)
        env = dict(self.env, XDG_CONFIG_HOME=str(config.parent), OPENAI_API_KEY='loopback-fixture-only')
        result = self.cli('start', '--no-attach', env=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('confirmation', result.stderr)
        self.assertTrue(probed.exists(), 'passive saved OMP selection was ignored')
        self.assertEqual(selection.read_bytes(), original)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertFalse((self.base / 'home/.local/share/omp-workbench/tools').exists())

    def test_managed_shell_own_supervisor_ignores_research_imports_and_preserves_child_env(self):
        import shlex
        import time
        from uuid import uuid4
        from workbench.terminal.shell_g2.prototype import ShellChoice
        from workbench.terminal.shell_persistent.adapter import PersistentShell
        poison = self.base / 'research modules'
        poison.mkdir()
        marker = self.base / 'unexpected-supervisor-import'
        (poison / 'sitecustomize.py').write_text(
            'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        result = self.base / 'research-result'
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(self.base / 'home'),
                       'PYTHONPATH': str(poison), 'VIRTUAL_ENV': '/research/venv'}
        shell = PersistentShell(user_environment=environment, choice=ShellChoice('bash', '/usr/bin/bash'))
        self.addCleanup(shell.close)
        def until(condition):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                value = shell.poll(.02)
                shell.display_bytes()
                if condition(value):
                    return value
            self.fail('managed shell lifecycle timeout: ' + repr(shell.snapshot()))
        shell.send_user(b'wb-handoff\n')
        until(lambda state: state['parent_mode'] == 'control_wait')
        shell.claim_manager()
        state = shell.snapshot()
        control = {'portVersion': 2, 'kind': 'ShellControl', 'payload': {
            'parentPid': shell.parent_pid, 'generation': state['generation'],
            'ownerEpoch': state['owner_epoch'], 'requestId': str(uuid4()),
            'approvalHash': 'e' * 64, 'phase': 'accepted'}}
        automation = {'portVersion': 2, 'kind': 'AutomationState', 'payload': {
            'paused': False, 'cancelled': False, 'metadataHealthy': True, 'approvalValid': True}}
        script = "printf '%s\\n' \"$PYTHONPATH\" \"$VIRTUAL_ENV\" > " + shlex.quote(str(result))
        shell.submit(control, script, automation)
        until(lambda state: state['lifecycle']['input_barrier'])
        shell.release_input()
        until(lambda state: state['lifecycle']['control_returned'])
        self.assertEqual(result.read_text().splitlines(), [str(poison), '/research/venv'])
        self.assertFalse(marker.exists(), 'WB supervisor imported research sitecustomize before main')

    def test_install_python_is_trusted_only_in_omp_and_research_host_environment_is_preserved(self):
        from workbench.backend.launcher import LaunchPlan, omp_environment, shell_environment
        from workbench.terminal.shell_g2.prototype import ShellChoice
        research = {'PATH': '/research/venv/bin:/usr/bin', 'VIRTUAL_ENV': '/research/venv',
                    'CONDA_PREFIX': '/research/conda', 'PYTHONPATH': '/research/modules',
                    'WORKBENCH_INSTALL_PYTHON': '/untrusted/inherited/python'}
        plan = LaunchPlan(ShellChoice('sh', '/usr/bin/sh'), '/fixture/omp', 'omp/18.12.2', '/fixture/bridge.ts', ())
        omp = omp_environment(research, plan, role='worker', token='fixture', bridge_socket=Path('/fixture/b.sock'))
        self.assertEqual(omp['WORKBENCH_INSTALL_PYTHON'], sys.executable)
        host = shell_environment(research)
        for key in ('PATH', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONPATH'):
            self.assertEqual(host[key], research[key])
        self.assertNotIn('WORKBENCH_INSTALL_PYTHON', host)

    def test_unsafe_backend_lock_is_an_identity_hold_and_preserves_user_file(self):
        legacy = self.base / 'legacy'
        legacy.mkdir(mode=0o700)
        referent = self.base / 'user-file'
        original = b'do not touch existing user data\n'
        referent.write_bytes(original)
        referent.chmod(0o600)
        (legacy / 'backend.lock').symlink_to(referent)
        result = self.cli('status', '--data-dir', str(legacy), '--json')
        self.assertEqual(result.returncode, 1)
        self.assertIn('identity', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(referent.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in legacy.iterdir()), ['backend.lock'])


if __name__ == '__main__':
    unittest.main()
