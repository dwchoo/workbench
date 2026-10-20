"""Explicit, transactional user-wide OMP preparation; no research or login turn."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, asdict
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from uuid import uuid4

from workbench.assets import canonical_path
from workbench.backend.install_recipe import inspect_existing, resolve_stable, download_verified
from workbench.backend.paths import write_private_json, write_private_bytes


class SetupError(ValueError):
    def __init__(self, code: str, *, path=None, detail=None):
        self.code = code
        self.path = str(path) if path is not None else None
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class SetupPaths:
    config: Path
    tools: Path

    @property
    def selection(self) -> Path:
        return self.config / 'setup.json'


def setup_paths(environment=None) -> SetupPaths:
    environment = os.environ if environment is None else environment
    home = Path(environment.get('HOME') or Path.home())
    if not home.is_absolute():
        raise SetupError('absolute_home_required')
    def base(name, fallback):
        supplied = Path(environment.get(name, ''))
        return supplied if supplied.is_absolute() else home / fallback
    return SetupPaths(base('XDG_CONFIG_HOME', '.config') / 'omp-workbench',
                      base('XDG_DATA_HOME', '.local/share') / 'omp-workbench/tools')


def _trusted_ancestor(fd):
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if (info.st_uid not in {0, os.geteuid()}
            or (mode & 0o022 and not mode & stat.S_ISVTX)):
        raise SetupError('unsafe_managed_ancestry')


def _directory(path: Path, *, create=False, private=False) -> int:
    """Walk by descriptors so an ancestor symlink cannot redirect managed writes."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _trusted_ancestor(fd)
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            _trusted_ancestor(fd)
        info = os.fstat(fd)
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise SetupError('unsafe_managed_directory')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_record(paths: SetupPaths, *, with_bytes=False):
    try:
        parent = _directory(paths.config, private=True)
    except FileNotFoundError:
        return (None, None) if with_bytes else None
    try:
        try:
            fd = os.open('setup.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            return (None, None) if with_bytes else None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
                raise SetupError('unsafe_setup_record', path=paths.selection)
            payload = os.read(fd, 65537)
            try:
                value = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise SetupError('invalid_setup_record', path=paths.selection, detail=str(error)) from error
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('state') != 'ready'
            or not isinstance(value.get('path'), str) or not Path(value['path']).is_absolute()):
        raise SetupError('invalid_setup_record', path=paths.selection)
    return (value, payload) if with_bytes else value


@dataclass(frozen=True)
class OMPSelection:
    path: Path
    source: str


def user_command(environment) -> Path:
    home = Path(environment.get('HOME') or Path.home())
    if not home.is_absolute():
        raise SetupError('absolute_home_required')
    return home / '.local/bin/omp'


def resolve_runtime_selection(omp=None, environment=None) -> OMPSelection:
    """Runtime keeps the stable command literal; setup provenance is not a fallback."""
    environment = os.environ if environment is None else environment
    for supplied, source in ((omp, 'explicit'), (environment.get('WORKBENCH_OMP'), 'environment')):
        if supplied:
            return OMPSelection(Path(supplied).absolute(), source)
    command = user_command(environment)
    try:
        parent = _directory(command.parent)
        try:
            # Sticky ancestors such as /tmp are valid, but the command directory
            # itself must not let another user create the default executable.
            if stat.S_IMODE(os.fstat(parent).st_mode) & 0o022:
                raise SetupError('unsafe_managed_ancestry', path=command)
        finally:
            os.close(parent)
    except (OSError, SetupError) as error:
        raise SetupError(getattr(error, 'code', 'unusable_user_command_parent'), path=command) from error
    return OMPSelection(command, 'user_bin')


def resolve_selection(omp=None, environment=None, *, prefer_user_command=False) -> OMPSelection | None:
    """Passive installation lookup; setup checks user-bin before saved/PATH.

    A selected missing/broken candidate remains selected; callers must report it,
    never silently replace it. This read performs no probe, mkdir or download.
    """
    environment = os.environ if environment is None else environment
    for supplied, source in ((omp, 'explicit'), (environment.get('WORKBENCH_OMP'), 'environment')):
        if supplied:
            return OMPSelection(Path(supplied).absolute(), source)
    if prefer_user_command and os.path.lexists(user_command(environment)):
        return OMPSelection(user_command(environment), 'user_bin')
    record = _read_record(setup_paths(environment))
    if record is not None:
        return OMPSelection(Path(record['path']), 'setup')
    found = shutil.which('omp', path=environment.get('PATH', ''))
    return OMPSelection(Path(found).absolute(), 'PATH') if found else None


@contextmanager
def _lock_directory(directory):
    parent = _directory(directory, create=True, private=True)
    fd = None
    try:
        fd = os.open('setup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise SetupError('unsafe_setup_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError('setup_in_progress') from error
        yield parent
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


@contextmanager
def _setup_lock(paths):
    # Config roots may differ while programs share XDG_DATA_HOME.
    with _lock_directory(paths.tools.parent) as shared:
        if paths.config == paths.tools.parent:
            yield shared
        else:
            with _lock_directory(paths.config) as parent:
                yield parent


def probe_sdk(binary: Path, environment, *, timeout=25) -> dict:
    """Public SDK imports + nonce readback in a credential-free one-shot home."""
    nonce = uuid4().hex
    with tempfile.TemporaryDirectory(prefix='wb-setup-probe-', dir=environment.get('TMPDIR')) as name:
        root = Path(name)
        env = {'PATH': environment.get('PATH', os.defpath), 'LANG': 'C.UTF-8',
               'PYTHONDONTWRITEBYTECODE': '1', 'WORKBENCH_SETUP_NONCE': nonce}
        for variable, directory in [('HOME', 'home'), ('TMPDIR', 'tmp'), ('PI_CODING_AGENT_DIR', 'agent'),
            ('PI_CONFIG_DIR', 'config'), ('XDG_CONFIG_HOME', 'xdg-config'), ('XDG_DATA_HOME', 'xdg-data'),
            ('XDG_STATE_HOME', 'xdg-state'), ('XDG_CACHE_HOME', 'xdg-cache'),
            ('BUN_RUNTIME_TRANSPILER_CACHE_PATH', 'transpiler')]:
            path = root / directory
            path.mkdir(mode=0o700)
            env[variable] = str(path)
        project = root / 'project'
        project.mkdir(mode=0o700)
        # A credential-free descriptor permits registry startup with no user login.
        # Its loopback endpoint is never prompted; this proves SDK load only.
        (root / 'agent/models.yml').write_text(
            'providers:\n  wb-setup:\n    api: openai-completions\n    auth: none\n'
            '    baseUrl: http://127.0.0.1:1/v1\n    models:\n      - id: load-only\n'
            '        name: load-only\n        contextWindow: 131072\n        maxTokens: 8192\n')

        argv = [str(binary), '--mode', 'rpc', '--thinking', 'off', '--model', 'wb-setup/load-only', '--no-session', '--no-title', '--no-extensions',
                '--no-skills', '--no-rules', '--no-pty', '--no-lsp', '--no-prewalk', '--no-tools',
                '--extension', str(canonical_path('omp_bridge/setup_probe.ts'))]
        process = subprocess.Popen(argv, cwd=project, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        deadline, total, buffer = time.monotonic() + timeout, 0, b''
        try:
            process.stdin.write(b'{"id":"wb-setup","type":"get_state"}\n')
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SetupError('sdk_load_timeout')
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > 131072:
                            raise SetupError('sdk_output_limit')
                        if key.data != 'stdout':
                            continue
                        buffer += chunk
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            try:
                                value = json.loads(line)
                            except (ValueError, UnicodeError):
                                continue
                            if isinstance(value, dict) and value.get('type') == 'workbench_setup_ready':
                                if value.get('nonce') != nonce or value.get('sdk') is not True:
                                    raise SetupError('sdk_load_failed')
                                return {'status': 'loaded', 'version': value.get('version'),
                                        'probe_sha256': sha256(canonical_path('omp_bridge/setup_probe.ts').read_bytes()).hexdigest()}
                raise SetupError('sdk_load_failed')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()



def _executable_path(path):
    try:
        binary = Path(path).resolve(strict=True)
    except FileNotFoundError as error:
        raise SetupError('existing_omp_missing', path=path) from error
    except (OSError, RuntimeError) as error:
        raise SetupError('unsafe_executable', path=path) from error
    parent = _directory(binary.parent)
    try:
        fd = os.open(binary.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                    or stat.S_IMODE(info.st_mode) & 0o022):
                raise SetupError('unsafe_executable', path=binary)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    return binary


def _checked_binary(binary, environment, expected_version=None):
    binary = _executable_path(binary)
    with tempfile.TemporaryDirectory(prefix='wb-setup-version-', dir=environment.get('TMPDIR')) as name:
        root = Path(name)
        version_env = {'PATH': environment.get('PATH', os.defpath), 'LANG': 'C.UTF-8'}
        for variable in ('HOME', 'TMPDIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
                         'XDG_CACHE_HOME', 'PI_CODING_AGENT_DIR', 'PI_CONFIG_DIR'):
            path = root / variable.lower()
            path.mkdir(mode=0o700)
            version_env[variable] = str(path)
        inspected = inspect_existing(binary, environment=version_env)
    if inspected.status != 'executable':
        raise SetupError('existing_omp_' + inspected.status, path=binary, detail=inspected.reason)
    before = sha256(binary.read_bytes()).hexdigest()
    try:
        sdk = probe_sdk(binary, environment)
    except SetupError as error:
        error.path = str(binary)
        raise
    if (sdk['version'] != inspected.version or (expected_version is not None and inspected.version != expected_version)
            or sha256(binary.read_bytes()).hexdigest() != before):
        raise SetupError('omp_changed_or_sdk_version_mismatch', path=binary)
    return inspected.version, before, sdk


def _command_link(record, environment):
    binary = Path(record['path'])
    command = user_command(environment)
    status = 'created'
    reason = warning = None
    try:
        parent = _directory(command.parent, create=True)
        try:
            try:
                os.symlink(str(binary), command.name, dir_fd=parent)
                status = 'created'
            except FileExistsError:
                try:
                    matching = os.path.samestat(os.stat(command.name, dir_fd=parent), binary.stat())
                except OSError:
                    matching = False
                if not matching:
                    raise SetupError('command_link_conflict', path=command)
                status = 'reused'
        finally:
            os.close(parent)
    except (OSError, SetupError) as error:
        status = 'warning'
        reason = getattr(error, 'code', type(error).__name__)
        warning = f'Could not prepare command link {command}: {reason}. OMP remains ready at {binary}.'
    found = shutil.which('omp', path=environment.get('PATH', ''))
    try:
        on_path = found is not None and Path(found).resolve(strict=True) == binary
    except (OSError, RuntimeError):
        on_path = False
    if on_path:
        guidance = 'Current PATH resolves omp to the selected OMP.'
    else:
        guidance = f'Current PATH does not resolve omp to the selected OMP (found: {found or "none"}).'
        if status in {'created', 'reused'}:
            guidance += f' For this shell, run: export PATH={shlex.quote(str(command.parent))}:"$PATH"'
        guidance += f' You can run the verified absolute executable instead: {shlex.quote(str(binary))}'
    return {'path': str(command), 'status': status, 'on_path': on_path,
            'resolved_path': found, 'reason': reason, 'warning': warning, 'guidance': guidance}


def _ready_result(record, status, command_link):
    return {'state': 'ready', 'status': status, **{key: record[key] for key in ('path','version','sdk')},
            'command_link': command_link,
            'real_models': 'unverified',
            'login_guidance': f"Run {'omp or ' if command_link['on_path'] else ''}{shlex.quote(record['path'])} "
                              'outside Workbench, use the OMP /login flow, then run wb. Login was not checked.',
            'update_guidance': 'Stop Workbench backends before explicitly updating this OMP installation.'}


def run_setup(omp=None, environment=None) -> dict:
    environment = dict(os.environ if environment is None else environment)
    paths = setup_paths(environment)
    installed = None
    committed = False
    retain_uncertain = False
    with _setup_lock(paths) as parent:
        try:
            selection = resolve_selection(omp, environment, prefer_user_command=True)
            recipe = None
            previous, previous_bytes = _read_record(paths, with_bytes=True)
            if selection is None:
                # Validate the Workbench-owned parent as well as the lock's config root.
                owned = _directory(paths.tools.parent, create=True, private=True)
                os.close(owned)
                tools = _directory(paths.tools, create=True, private=True)
                try:
                    recipe = resolve_stable()
                    with tempfile.TemporaryDirectory(prefix='.staging-', dir=paths.tools) as name:
                        staged = Path(name) / 'omp'
                        download_verified(recipe, staged)
                        version, digest, sdk = _checked_binary(staged, environment, recipe.version)
                        installed = Path(tempfile.mkdtemp(prefix=f'{version}-{digest[:12]}-', dir=paths.tools))
                        binary = installed / 'omp'
                        # A newly owned unique version prefix, never an existing installation.
                        os.link(staged, binary, follow_symlinks=False)
                        staged.unlink()
                        final = _directory(installed, private=True)
                        try:
                            os.fsync(final)
                        finally:
                            os.close(final)
                        os.fsync(tools)
                finally:
                    os.close(tools)
            else:
                binary = _executable_path(selection.path)
                version, digest, sdk = _checked_binary(binary, environment)
            record = {'schema_version': 1, 'state': 'ready', 'path': str(binary), 'version': version,
                      'binary_sha256': digest, 'source': selection.source if selection else 'managed',
                      'method': 'official_binary' if recipe else 'existing', 'sdk': sdk,
                      'recipe': asdict(recipe) if recipe else None}
            if previous and previous['path'] == str(binary) and previous.get('binary_sha256') == digest:
                record.update(method=previous.get('method', 'existing'), recipe=previous.get('recipe'))
            try:
                write_private_json(paths.selection, record, dir_fd=parent)
                committed = True
            except BaseException:
                committed = False
                try:
                    if previous_bytes is not None:
                        write_private_bytes(paths.selection, previous_bytes, dir_fd=parent)
                    else:
                        try:
                            os.unlink('setup.json', dir_fd=parent)
                        except FileNotFoundError:
                            pass
                        os.fsync(parent)
                except BaseException as rollback_error:
                    retain_uncertain = True
                    raise SetupError('selection_rollback_failed', path=paths.selection,
                                     detail='Repair the filesystem and inspect the retained setup selection.') from rollback_error
                raise
            command_link = _command_link(record, environment)
            return _ready_result(record, 'installed' if installed else 'reused', command_link)
        finally:
            if installed is not None and not committed and not retain_uncertain:
                shutil.rmtree(installed)
