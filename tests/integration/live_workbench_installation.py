"""WI-08 final installed-artifact integration; explicit owned fixtures only.

Reuses the reviewed installation lifecycle/migration methods. No historical RED
is claimed: the feature already exists; this requirement-derived harness observes
its current installed behavior. Runtime subprocesses use -I, private HOME/XDG,
loopback fake auth, and no checkout imports. Root owns independent final review.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/backend/installation'))
import live_project_sessions as sessions
import live_migration as migration


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def canonical_inputs():
    result = {}
    for base in ('src', 'omp_bridge', 'build_support'):
        for path in sorted((ROOT / base).rglob('*')):
            if path.is_file() and not any(part in ('__pycache__', '_assets') or part.endswith('.egg-info')
                                          for part in path.relative_to(ROOT).parts) and path.suffix != '.pyc':
                result[str(path.relative_to(ROOT))] = digest(path.read_bytes())
    for name in ('pyproject.toml', 'MANIFEST.in'):
        result[name] = digest((ROOT / name).read_bytes())
    return result


INSTALLED = r'''
import importlib.metadata as m,json,sys
from hashlib import sha256
from pathlib import Path
from workbench.assets import package_root,canonical_path
from workbench.backend.summary_compatibility import collect_runtime_identity,SummaryCompatibility
from workbench.backend import launcher
package=package_root();identity=collect_runtime_identity()
assert identity['unknowns']==[],identity['unknowns']
service=SummaryCompatibility(Path(sys.argv[1])/'preflight-uninitialized')
assert not service.admission() and not service.activation_preflight('00000000-0000-4000-8000-000000000000')
assert not service.directory.exists(), 'passive guard allocated a store'
assert set(launcher.role_skill_allowlist('manager'))=={'to-worker','workbench-recovery','workbench-report'}
assert set(launcher.role_skill_allowlist('worker'))=={'to-manager'}
skill=canonical_path('omp_bridge/skills/workbench-report/SKILL.md').read_text()
assert 'disableModelInvocation: true' in skill.split('---',2)[1]
assert launcher.WORKER_BRIDGE_TOOLS=={'to_manager','terminal','workbench_logs'}
dists=[]
for d in m.distributions():
 metadata=d.read_text('METADATA')
 dists.append({'name':d.metadata['Name'],'version':d.version,'metadata':metadata,
               'requires':d.requires or [],'metadata_sha256':sha256(metadata.encode()).hexdigest()})
print(json.dumps({'package':str(package),'python':sys.executable,'sys_path':sys.path,
 'identity':identity,'direct_url':json.loads(m.distribution('workbench').read_text('direct_url.json')),
 'distributions':dists,'payload':{str(p.relative_to(package)):sha256(p.read_bytes()).hexdigest()
  for p in package.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'},
 'preflight_unverified_denied':True,'manager_report_user_only_worker_not_exposed':True}))
'''


class FinalRun(sessions.InstalledRun):
    artifact = 'git'
    prepared = None

    def install(self):
        bundle = self.prepared
        self.report['artifact_kind'] = self.artifact
        self.env.update(UV_OFFLINE='1', UV_NO_PROGRESS='1', UV_LINK_MODE='copy')
        # Read the existing dependency cache through an owned copy. No global setup.
        shutil.copytree(bundle['cache'], Path(self.env['UV_CACHE_DIR']), dirs_exist_ok=True, symlinks=False)
        source = bundle[self.artifact]
        self.run([bundle['uv'], 'tool', 'install', '--offline',
                  '--python', sys.executable, source], 'install-' + self.artifact)
        self.env['WORKBENCH_OMP'] = str(self.omp)
        self.env['WORKBENCH_OMP_ARGS'] = '--no-pty --no-rules --model wi-project/scripted --config ' + str(self.model)
        for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            self.env[key] = 'http://127.0.0.1:9'
        self.env.update(NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        auth = Path(self.env['HOME']) / '.omp/agent'
        auth.mkdir(parents=True, mode=0o700)
        (auth / 'agent.db').write_bytes(b'')
        _, output, _ = self.run([self.python, '-I', '-c', INSTALLED, self.private], 'installed-closure')
        installed = json.loads(output)
        package = Path(installed['package'])
        assert package.is_relative_to(self.private / 'uv_tool_dir')
        assert all(not str(path).startswith(str(ROOT)) for path in installed['sys_path'])
        assert installed['payload'] == bundle['payload'], 'installed entire package payload differs from wheel'
        direct = installed['direct_url']
        assert not direct.get('dir_info', {}).get('editable', False)
        if self.artifact == 'git':
            assert direct['vcs_info']['commit_id'] == bundle['commit']
        else:
            assert direct['url']==Path(source).as_uri(),direct
            assert digest(Path(source).read_bytes())==bundle['artifacts'][Path(source).name]
        for relative, expected in bundle['canonical'].items():
            if relative.startswith('src/workbench/'):
                actual = package / relative.removeprefix('src/workbench/')
            elif relative.startswith('omp_bridge/') or relative == 'pyproject.toml':
                actual = package / '_assets' / relative
            else:
                continue
            assert digest(actual.read_bytes()) == expected, relative
        for member, expected in bundle['wheel_members'].items():
            if member.startswith('workbench-') and '/METADATA' in member:
                assert digest((package.parent / member).read_bytes()) == expected
            if '.data/scripts/' in member:
                target = self.python.parent / member.rsplit('/', 1)[1]
                assert digest(target.read_bytes()) == expected, member
        self.report['installed'] = installed
        save(self.output / 'installed-full-closure.json', installed)
        for row in installed['distributions']:
            (self.output / (row['name'] + '-METADATA')).write_text(row['metadata'])
        _, text, _ = self.run([self.command, 'setup', '--omp', self.omp, '--json'], 'setup-explicit', timeout=100)
        setup = json.loads(text)
        assert setup['status'] == 'reused' and setup['sdk']['status'] == 'loaded', setup
        assert setup['real_models'] == 'unverified'
        _, text, _ = self.run([self.command, 'setup', '--json'], 'setup-reuse', timeout=100)
        assert json.loads(text)['path'] == str(self.omp)
        self.report['setup'] = setup
        assert self.provider.requests == 0
        for name in ('wb', 'omp-workbench'):
            self.run([self.command.with_name(name), '--help'], name + '-help')
        self.report['checks'].update(full_installed_payload_and_metadata=True, source_independent_no_pythonpath=True,
            installed_fingerprint_and_unverified_preflight=True, actual_setup_sdk_reuse_no_model=True,
            Manager_report_user_only_Worker_not_exposed=True)

    def run(self, argv, name, **kwargs):
        # During migration and lifecycle exercise all public status spellings.
        result = super().run(argv, name, **kwargs)
        if name == 'legacy-explicit-status':
            project = kwargs['cwd']
            data = Path(argv[3])
            for label, command, environment in (
                ('alias', [self.command.with_name('omp-workbench'),'status','--json','--data-dir',data], self.env),
                ('flag-before', [self.command,'--data-dir',data,'status','--json'], self.env),
                ('env', [self.command,'status','--json'], dict(self.env,WORKBENCH_DATA_DIR=str(data)))):
                code, out, error = super().run(command, 'legacy-' + label, cwd=project, env=environment, check=label!='flag-before')
                if label=='flag-before':
                    assert code==2 and 'invalid choice' in error, (code,out,error)
                else:
                    assert json.loads(out)['snapshot']['backend']['pid'] == json.loads(result[1])['snapshot']['backend']['pid']
        return result

    def bare_first_affirm(self, project):
        """Use inherited real PTY/drain cleanup, supply y only to its init prompt."""
        # The inherited refusal path is otherwise identical: start --no-attach.
        # A dedicated subprocess below uses the public bare command and detaches.
        import fcntl, select, signal, struct, termios, threading
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH',32,110,0,0))
        child = subprocess.Popen(['/usr/bin/setsid','--ctty',str(self.command)], cwd=project, env=self.env,
                                 stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        output = bytearray(); stop = threading.Event()
        def drain():
            while not stop.is_set():
                try:
                    if select.select([master],[],[],.05)[0]:
                        chunk=os.read(master,65536)
                        if not chunk:break
                        output.extend(chunk)
                except OSError:break
        reader=threading.Thread(target=drain,daemon=True);reader.start()
        try:
            deadline=time.monotonic()+15
            while b'[y/N]' not in output:
                assert time.monotonic()<deadline,bytes(output[-2000:])
                time.sleep(.05)
            assert os.fsencode(project) in output and not (project/'.wb').exists()
            os.write(master,b'y\r')
            deadline=time.monotonic()+130; number=0
            while time.monotonic()<deadline:
                code,text,_=self.run([self.command,'status','--json'],f'first-affirm-status-{number}',cwd=project,check=False)
                number+=1
                if code==0 and json.loads(text)['snapshot']['attached']:break
                assert child.poll() is None,bytes(output[-3000:])
                time.sleep(.1)
            else:raise AssertionError('affirmative bare wb did not attach')
            os.write(master,b'\x1dq');assert child.wait(timeout=15)==0
            self.report['checks']['bare_first_root_affirm_and_detach']=True
        finally:
            if child.returncode is None:
                os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=10)
            stop.set();reader.join(2);os.close(master)
            (self.output/'bare-first-affirm.pty').write_bytes(output)

    def report_turn(self, project):
        """The real user explicitly invokes report; loopback requests installed helper."""
        draft = Path(self.report['installed']['package'])/'_assets/omp_bridge/skills/workbench-report/references/report-template.md'
        helper = draft.parent.parent/'scripts/report_io.py'
        target=project/'bug-reports/WI08-explicit-user.md'
        import shlex
        self.provider.helper_command = ('"${WORKBENCH_INSTALL_PYTHON:?WB installation Python unavailable}" -I '
            + shlex.quote(str(helper)) + ' --project-dir ' + shlex.quote(str(project))
            + ' --output ' + shlex.quote(str(target)) + ' < ' + shlex.quote(str(draft)))
        code=migration.TURN.replace("'WI06_MIGRATE_TURN'", "'/skill:workbench-report save WI08 explicit user report'")
        before=self.provider.requests
        self.run([self.python,'-I','-c',code,project/'.wb'],'explicit-user-Manager-report',timeout=50)
        assert self.provider.requests==before+2
        assert target.is_file() and target.read_bytes()==draft.read_bytes()
        assert target.stat().st_mode & 0o777==0o600
        self.report['report_artifact']={'path':str(target),'sha256':digest(target.read_bytes()),'explicit_user':True}
        del self.provider.helper_command
        self.report['checks']['actual_Manager_explicit_user_installed_helper_fixed_save']=True

    def cleanup(self):
        super().cleanup()
        assert not self.report['remaining_processes'] and not self.report['unrecorded_private_processes'],self.report
        assert not self.thread.is_alive()
        # Only own native/model cache after verified process zero. Preserve DB/session/evidence.
        removed=[]
        for path in (Path(self.env['HOME'])/'.omp/natives',Path(self.env['XDG_CACHE_HOME'])):
            if path.exists():
                size=sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())
                shutil.rmtree(path);removed.append({'path':str(path),'logical_bytes':size})
        self.report['own_cache_cleanup']=removed
        self.report['retained_logical_bytes']=sum(p.stat().st_size for p in self.private.rglob('*')
                                                  if p.is_file() and not p.is_symlink())
        self.report['owner_uid']=os.geteuid();self.report['process_state']='STOPPED'
        save(self.output/'receipt.json',self.report)


def prepare(run):
    source=run.private/'canonical-git';source.mkdir()
    expected=canonical_inputs()
    for name in ('src','omp_bridge','build_support'):
        shutil.copytree(ROOT/name,source/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.egg-info','_assets'))
    for name in ('pyproject.toml','MANIFEST.in'):shutil.copy2(ROOT/name,source/name)
    assert {key:digest((source/key).read_bytes()) for key in expected}==expected
    uv=shutil.which('uv');assert uv
    # Copy retained dependency artifacts; UV_OFFLINE forbids any new download.
    run.env.update(UV_OFFLINE='1',UV_NO_PROGRESS='1',GIT_AUTHOR_NAME='WI08 owned fixture',
        GIT_AUTHOR_EMAIL='wi08@example.invalid',GIT_COMMITTER_NAME='WI08 owned fixture',GIT_COMMITTER_EMAIL='wi08@example.invalid')
    cache=run.private/'dependency-cache'
    shutil.copytree(ROOT/'.workflow/WI-02-independent-tests/git-install-fu6zu538/uv-cache',cache,symlinks=False)
    shutil.copytree(cache,Path(run.env['UV_CACHE_DIR']),dirs_exist_ok=True)
    for argv,name in ((['/usr/bin/git','-c','core.hooksPath=/dev/null','init',source],'git-init'),
        (['/usr/bin/git','-C',source,'-c','core.hooksPath=/dev/null','add','--all'],'git-add'),
        (['/usr/bin/git','-C',source,'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-m','owned current canonical fixture'],'git-commit')):
        run.run(argv,name)
    _,text,_=run.run(['/usr/bin/git','-C',source,'rev-parse','HEAD'],'git-ref');commit=text.strip()
    dist=run.output/'dist'
    run.run([uv,'build','--offline','--python',sys.executable,
             '--sdist','--wheel','--out-dir',dist,source],'build-current',timeout=180)
    wheel=next(dist.glob('*.whl'));sdist=next(dist.glob('*.tar.gz'))
    with zipfile.ZipFile(wheel) as archive:
        members={name:digest(archive.read(name)) for name in archive.namelist() if not name.endswith('/')}
        payload={name.removeprefix('workbench/'):value for name,value in members.items() if name.startswith('workbench/')}
    with tarfile.open(sdist) as archive:
        sdist_members={p.name:digest(archive.extractfile(p).read()) for p in archive.getmembers() if p.isfile()}
    artifacts={p.name:digest(p.read_bytes()) for p in (wheel,sdist)}
    setuptools_metadata=next((cache/'archive-v0').glob('*/setuptools-84.0.0.dist-info/METADATA')).read_text()
    (run.output/'setuptools-build-METADATA').write_text(setuptools_metadata)
    bundle={'uv':uv,'cache':str(cache),'git':'git+'+source.as_uri()+'@'+commit,'commit':commit,
        'wheel':str(wheel),'sdist':str(sdist),'canonical':expected,'artifacts':artifacts,
        'canonical_input_digest':digest(json.dumps(expected,sort_keys=True,separators=(',',':')).encode()),
        'wheel_members':members,'payload':payload,'sdist_members':sdist_members,
        'build_dependencies':{'setuptools':'84.0.0','metadata_sha256':digest(setuptools_metadata.encode())},
        'offline_cache_source':str(ROOT/'.workflow/WI-02-independent-tests/git-install-fu6zu538/uv-cache'),
        'source':str(source),'harness_python':sys.executable,'runtime_scenario':{'git':'lifecycle+PTY+Manager report','wheel':'migration+public readers','sdist':'setup+SDK+payload+preflight'}}
    save(run.output/'artifact-input-manifest.json',bundle)
    return bundle


def lifecycle(run):
    run.install()
    # Original fixture path disappears after Git installation; all later behavior
    # uses the installed package and exact byte manifest, with no source PYTHONPATH.
    Path(run.prepared['source']).rename(run.private/'source-unavailable')
    run.research_environment()
    refused=run.private/'first-refused';refused.mkdir();run.bare_pty(refused,'bare_first_default_no',refusal=True)
    project=run.private/'research-A';project.mkdir();run.projects.append(project)
    run.run(['/usr/bin/git','init',project],'research-git-init')
    (project/'experiments').mkdir()
    nested,context=run.project('research-A/experiments/nested')
    run.bare_first_affirm(project)
    run.run(['/usr/bin/git','-C',project,'check-ignore','.wb/'],'project-git-ignored')
    child=project/'experiments'
    snap=run.status(child,'status-A');record=json.loads((project/'.wb/backend.json').read_text())
    assert snap['backend']['project_dir']==str(project)
    assert snap['phase']=='ready'
    run.report['bare_first_snapshot']=snap
    run.report['bare_first_health']='ready is not an OMP isolation/provider health claim; no seeded agent/models.yml yet'
    run.run([run.command,'shutdown','--yes','--json'],'bare-first-confirmed-stop',cwd=project,timeout=65)
    deadline=time.monotonic()+6
    while any(sessions.state(ref['pid'],ref['start_ticks'])=='alive' for ref in record['processes'].values()) and time.monotonic()<deadline:
        time.sleep(.03)
    assert all(sessions.state(ref['pid'],ref['start_ticks'])=='ended' for ref in record['processes'].values())
    (project/'.wb/omp-root/agent/models.yml').write_bytes(run.model.read_bytes())
    run.start(project,'seeded-controlled-A-start')
    snap=run.status(child,'seeded-controlled-A-status');record=json.loads((project/'.wb/backend.json').read_text())
    assert snap['phase']=='ready' and snap['omp_isolation']['checked'] and snap['omp_isolation']['ok'],snap['omp_isolation']
    assert all(snap['bridge'][role]['connected'] and snap['bridge'][role]['pid_matches_pane'] for role in ('manager','worker'))
    assert run.provider.requests==0
    run.environment_checks(project,snap)
    run.report_turn(project)
    run.bare_pty(child,'bare-subdir-attach-detach')
    assert run.status(project,'status-after-detach')['automation']['paused']
    starting_before=(project/'.wb/backend.log').read_text().count('starting in')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run.start,project,'concurrent-A-'+str(i),cwd=child) for i in range(2)]
        for future in futures:future.result()
    assert (project/'.wb/backend.log').read_text().count('starting in')==starting_before
    project_B,context_B=run.project('연구-'+('긴경로'*20))
    assert len(os.fsencode(project_B/'.wb/ui.sock'))>107
    run.start(project_B,'long-B-start');snap_B=run.status(project_B,'long-B-status')
    record_B=json.loads((project_B/'.wb/backend.json').read_text())
    assert snap_B['phase']=='ready' and snap_B['omp_isolation']['ok']
    assert record_B['project_id']!=record['project_id'] and record_B['runtime_root']!=record['runtime_root']
    run.exercise(project_B,context_B,'B')
    for name in ('tasks.sqlite3','important-logs','raw-logs'):
        assert not (project/'.wb'/name).samefile(project_B/'.wb'/name)
    for label,path in (('A',project),('B',project_B)):
        run.run([run.command,'logs','raw-status','--json'],'raw-status-'+label,cwd=path)
    a=b''.join(p.read_bytes() for p in (project/'.wb/omp-root/agent/sessions').rglob('*.jsonl'))
    b=b''.join(p.read_bytes() for p in (project_B/'.wb/omp-root/agent/sessions').rglob('*.jsonl'))
    assert b'WI08 explicit user report' in a and b'WI05_LOOPBACK_TURN_B' not in a
    assert b'WI05_LOOPBACK_TURN_B' in b and b'WI08 explicit user report' not in b
    run.bound_sockets([record,record_B])
    run.restart_different_runtime(project)
    # Public legacy alias/flag/env priority, nearest nested project and invalid marker.
    run.run([run.command,'status','--data-dir',project/'.wb','--json'],'legacy-explicit-status',cwd=project_B)
    env=dict(run.env,WORKBENCH_DATA_DIR=str(project_B/'.wb'))
    _,text,_=run.run([run.command,'status','--data-dir',project/'.wb','--json'],'flag-beats-env',cwd=project_B,env=env)
    assert json.loads(text)['snapshot']['backend']['project_dir']==str(project)
    _,text,_=run.run([run.python,'-I','-c',
        'import os,json;from pathlib import Path;from workbench.backend.project import resolve_project_context;c=resolve_project_context(Path.cwd(),os.environ);print(json.dumps({"project":str(c.project_root)}))'],
        'nearest-nested-project',cwd=nested)
    assert json.loads(text)['project']==str(nested)
    invalid=child/'invalid';invalid.mkdir();(invalid/'.wb').mkdir();(invalid/'.wb/user.txt').write_text('preserve')
    code,_,_=run.run([run.command,'status','--json'],'invalid-nearest-refused',cwd=invalid,check=False)
    assert code!=0 and (invalid/'.wb/user.txt').read_text()=='preserve'
    assert run.provider.requests==3
    run.report['records']=[record,record_B]
    run.report['checks'].update(project_AB_public_history_separation=True,long_UDS_bound=True,
        concurrent_start_single_backend=True,nearest_nested_and_invalid_marker=True,
        alias_flags_env_precedence=True,detach_pause_reconnect_restart_no_replay=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root',required=True,type=Path)
    parser.add_argument('--omp',required=True,type=Path)
    parser.add_argument('--artifact-manifest',type=Path,help='reuse an exact unchanged owned artifact after a failed harness attempt')
    parser.add_argument('--completed-git-receipt',type=Path,help='reuse a completed exact-artifact Git lifecycle without repeating it')
    args=parser.parse_args();output=args.run_root.absolute();output.mkdir(mode=0o700,parents=True,exist_ok=False)
    size=subprocess.run(['du','-sh',str(ROOT/'.workflow'),str(ROOT/'graphify-out')],capture_output=True,text=True,check=True)
    (output/'storage-before.txt').write_text(size.stdout)
    amount=int(subprocess.check_output(['du','-sb',str(ROOT/'.workflow')]).split()[0]);assert amount<10*1024**3
    receipts=[];bundle=None
    if args.artifact_manifest:
        bundle=json.loads(args.artifact_manifest.read_text())
        assert bundle['canonical']==canonical_inputs(),'reused artifact source candidate changed'
        assert all(digest(Path(bundle[k]).read_bytes())==bundle['artifacts'][Path(bundle[k]).name] for k in ('wheel','sdist'))
        save(output/'reused-artifact.json',{'path':str(args.artifact_manifest.absolute()),'sha256':digest(args.artifact_manifest.read_bytes())})
    else:
        build=FinalRun(output/'build',args.omp)
        try:
            bundle=prepare(build)
        except BaseException as error:
            build.report['error']=f'{type(error).__name__}: {error}';raise
        finally:
            build.cleanup();receipts.append(str(build.output/'receipt.json'))
    FinalRun.prepared=bundle
    # Git ref must remain available until uv completes its actual installation.
    if args.completed_git_receipt:
        receipt=json.loads(args.completed_git_receipt.read_text())
        assert receipt['error'] is None and receipt['artifact_kind']=='git'
        assert receipt['installed']['payload']==bundle['payload']
        assert receipt['installed']['direct_url']['vcs_info']['commit_id']==bundle['commit']
        assert receipt['checks']['detach_pause_reconnect_restart_no_replay'] is True
        assert receipt['checks']['actual_Manager_explicit_user_installed_helper_fixed_save'] is True
        assert not receipt['remaining_processes'] and not receipt['unrecorded_private_processes']
        receipts.append(str(args.completed_git_receipt.absolute()))
        save(output/'reused-git-receipt.json',{'path':str(args.completed_git_receipt.absolute()),'sha256':digest(args.completed_git_receipt.read_bytes())})
    else:
        gitrun=FinalRun(output/'git-lifecycle',args.omp)
        try:
            lifecycle(gitrun)
        except BaseException as error:
            gitrun.report['error']=f'{type(error).__name__}: {error}';raise
        finally:
            gitrun.cleanup();receipts.append(str(gitrun.output/'receipt.json'))
    # Remove the now hidden Git source before subsequent artifact-only installs.
    assert not Path(bundle['source']).exists()
    FinalRun.artifact='sdist'
    sdist=FinalRun(output/'sdist-installed',args.omp)
    try:sdist.install()
    except BaseException as error:
        sdist.report['error']=f'{type(error).__name__}: {error}';raise
    finally:
        sdist.cleanup();receipts.append(str(sdist.output/'receipt.json'))
    FinalRun.artifact='wheel'
    migration.InstalledRun=FinalRun
    old_argv=sys.argv
    try:
        sys.argv=[str(Path(migration.__file__)),'--run-root',str(output/'wheel-migration'),'--omp',str(args.omp)]
        migration.main()
    finally:sys.argv=old_argv
    receipts.append(str(output/'wheel-migration/receipt.json'))
    save(output/'result.json',{'status':'candidate_ready','process_state':'STOPPED','receipts':receipts,
        'artifact_manifest':str(args.artifact_manifest.absolute() if args.artifact_manifest else output/'build/artifact-input-manifest.json'),
        'TDD_exception':'Existing implemented feature; requirement-first actual installed integration, no retrospective RED or production delta.',
        'limits':['Linux x86_64 glibc/local filesystem only','fake auth and controlled loopback provider only',
                  'No paid provider/user authentication/foreign UID/outer multiplexer claim','G-WI-11 independent final review remains Root-owned']})
    (output/'storage-after.txt').write_text(subprocess.check_output(['du','-sh',str(ROOT/'.workflow'),str(ROOT/'graphify-out')],text=True))
    print(json.dumps({'status':'candidate_ready','output':str(output),'state':'STOPPED'}))


if __name__=='__main__':main()
