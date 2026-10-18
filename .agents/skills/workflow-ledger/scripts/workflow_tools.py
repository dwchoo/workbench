"""Shared local workflow evidence primitives. No scheduler; restore is delta-only and opt-in."""
from __future__ import annotations

import argparse
import base64
import fcntl
import fnmatch
import hashlib
import json
import math
from collections import deque, namedtuple
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def safe_path(root, relative):
    relative = Path(relative)
    require(not relative.is_absolute() and '..' not in relative.parts, 'Expected project-relative path')
    result = root / relative
    require(result.parent.resolve().is_relative_to(root.resolve()), 'Path escapes project')
    return result


def atomic(path, value):
    path = Path(path)
    require(not path.is_symlink(), 'Refuse symlink output')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def exclusive_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())


# Path observations are local to one preflight, never a lock-time cache.
PathIdentity = namedtuple('PathIdentity', 'lexical resolved inode uncertain')


def inspect_path_identity(path):
    lexical = Path(path).absolute()  # Preserve symlink/.. filesystem semantics.
    resolved, inode, uncertain = None, None, False
    try:
        resolved = lexical.resolve()
    except (OSError, RuntimeError, ValueError):
        uncertain = True
    try:
        info = lexical.stat()
        inode = (info.st_dev, info.st_ino)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        uncertain = True
    return PathIdentity(lexical, resolved, inode, uncertain)


def path_relation(left, right):
    if left.lexical == right.lexical:
        return 'same'
    if left.inode is not None and left.inode == right.inode:
        return 'alias'
    pairs = []
    if '..' not in left.lexical.parts and '..' not in right.lexical.parts:
        pairs.append((left.lexical, right.lexical))
    if left.resolved is not None and right.resolved is not None:
        pairs.append((left.resolved, right.resolved))
    for first, second in pairs:
        if first == second:
            return 'same'
        if second.is_relative_to(first):
            return 'ancestor'
        if first.is_relative_to(second):
            return 'descendant'
    return 'undetermined' if left.uncertain or right.uncertain else 'disjoint'


def paths_overlap(left, right):
    relation = path_relation(inspect_path_identity(left), inspect_path_identity(right))
    # Conversion's existing adapter handles unavailable paths as invalid requests.
    return relation != 'disjoint'


def ledger_lock_path(path):
    return Path(str(path) + '.lock')


def git(root, *args, optional=False):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    if result.returncode and not optional:
        raise ValueError(result.stderr.decode(errors='replace'))
    return result.stdout if result.returncode == 0 else b''


def matches(path, patterns):
    return any(p == '.' or path == p or path.startswith(p.rstrip('/') + '/') or
               fnmatch.fnmatchcase(path, p) for p in patterns)


def capture(root, watch=(), exclude=(), blobs=None):
    root = Path(root).resolve()
    require(git(root, 'rev-parse', '--show-toplevel').decode().strip() == str(root), 'Use Git workspace root')
    names = set(os.fsdecode(p) for p in git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0') if p)
    for name in watch:
        path = safe_path(root, name)
        if path.is_dir() and not path.is_symlink():
            for folder, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = [d for d in dirs if d != '.git']
                names.update((Path(folder) / p).relative_to(root).as_posix() for p in files)
                names.update((Path(folder) / p).relative_to(root).as_posix() for p in dirs if (Path(folder) / p).is_symlink())
        else:
            names.add(name)
    files, unknowns = {}, []
    blob_bytes = 0
    def content(raw):
        nonlocal blob_bytes
        key = hashlib.sha256(raw).hexdigest()
        if blobs is not None and key not in blobs:
            # A checkpoint is a bounded recovery copy, not an output archive.
            require(blob_bytes + len(raw) <= 32 * 1024 * 1024,
                    'Checkpoint recovery data exceeds 32 MiB; narrow watch/exclude '
                    'and exclude generated output, caches and runtime fixtures')
            blobs[key] = base64.b64encode(raw).decode()
            blob_bytes += len(raw)
        return key
    for name in sorted(names):
        if '.git' in Path(name).parts or matches(name, exclude):
            continue
        path = safe_path(root, name)
        try:
            info = path.lstat()
        except FileNotFoundError:
            files[name] = {'kind': 'deleted'}
            continue
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            files[name] = {'kind': 'symlink', 'mode': mode, 'target': os.readlink(path)}
        elif stat.S_ISREG(info.st_mode):
            files[name] = {'kind': 'file', 'mode': mode, 'sha256': content(path.read_bytes())}
        elif stat.S_ISDIR(info.st_mode):
            files[name] = {'kind': 'submodule', 'head': git(path, 'rev-parse', 'HEAD', optional=True).decode().strip(),
                           'status': git(path, 'status', '--porcelain=v1', '--untracked-files=all', optional=True).decode(errors='replace')}
            if files[name]['status']:
                unknowns.append('dirty submodule requires separate snapshot: ' + name)
        else:
            unknowns.append('unsupported input: ' + name)
    index = []
    for raw in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if not raw:
            continue
        meta, raw_name = raw.split(b'\t', 1)
        mode, oid, stage = meta.decode().split()
        name = os.fsdecode(raw_name)
        if matches(name, exclude):
            continue
        entry = {'path': name, 'mode': mode, 'oid': oid, 'stage': stage}
        if mode != '160000':
            entry['sha256'] = content(git(root, 'cat-file', 'blob', oid))
        index.append(entry)
    identity = {'files': files, 'index': index, 'head': git(root, 'rev-parse', 'HEAD', optional=True).decode().strip(),
                'watch': sorted(watch), 'exclude': sorted(exclude)}
    return {'schema_version': 1, 'candidate': digest(identity), 'identity': identity, 'unknowns': unknowns}


def snapshot(root, watch=(), exclude=(), checkpoint=False):
    blobs = {} if checkpoint else None
    before = capture(root, watch, exclude, blobs)
    after = capture(root, watch, exclude)
    result = {**before, 'observed_at': now(), 'workspace': str(Path(root).resolve()),
              'stable': before['candidate'] == after['candidate'] and not before['unknowns']}
    if checkpoint:
        result['blobs'] = blobs
    return result


def audit(before, after, allowed):
    a, b = before['identity']['files'], after['identity']['files']
    changed = sorted(name for name in a.keys() | b.keys() if a.get(name) != b.get(name))
    index_changed = sorted({e['path'] for e in before['identity']['index'] + after['identity']['index']
                            if e not in before['identity']['index'] or e not in after['identity']['index']})
    return {'changed': changed, 'index_changed': index_changed,
            'outside_scope': [p for p in sorted(set(changed + index_changed)) if not matches(p, allowed)],
            'comparable': all(before['identity'][k] == after['identity'][k] for k in ('watch', 'exclude'))}


DELETED = {'kind': 'deleted'}


def restore_plan(pre, post, current):
    """Classify the integration delta (pre -> post) against the current workspace."""
    pf, qf, cf = pre['identity']['files'], post['identity']['files'], current['identity']['files']
    delta = sorted(n for n in pf.keys() | qf.keys() if pf.get(n, DELETED) != qf.get(n, DELETED))
    pi = {(e['path'], e['stage']): e for e in pre['identity']['index']}
    qi = {(e['path'], e['stage']): e for e in post['identity']['index']}
    ci = {(e['path'], e['stage']): e for e in current['identity']['index']}
    index_delta = sorted({k[0] for k in pi.keys() | qi.keys() if pi.get(k) != qi.get(k)})
    conflicts = []
    for name in sorted(set(delta) | set(index_delta)):
        stages = {k[1] for k in ci.keys() | qi.keys() if k[0] == name}
        if name in delta and cf.get(name, DELETED) != qf.get(name, DELETED):
            conflicts.append({'path': name, 'reason': 'worktree changed after integration'})
        elif any(ci.get((name, st)) != qi.get((name, st)) for st in stages):
            conflicts.append({'path': name, 'reason': 'index changed after integration'})
        elif any(k[0] == name and k[1] != '0' for k in pi):
            conflicts.append({'path': name, 'reason': 'pre-integration index has merge stages'})
        elif 'submodule' in (pf.get(name, DELETED)['kind'], qf.get(name, DELETED)['kind']):
            conflicts.append({'path': name, 'reason': 'submodule paths are not restorable'})
    return delta, index_delta, conflicts


def restore(root, pre, post, watch=None, exclude=None, apply=False):
    """Undo only the integration delta. Dry-run unless apply is true; never reset/clean/stash."""
    root = Path(root).resolve()
    pre, post = read(pre), read(post)
    require(pre.get('blobs') is not None, 'Pre-integration manifest must be a checkpoint with blobs')
    for key in ('watch', 'exclude'):
        require(pre['identity'][key] == post['identity'][key], 'Pre and post manifests use different ' + key + ' policy')
    watch = pre['identity']['watch'] if watch is None else watch
    exclude = pre['identity']['exclude'] if exclude is None else exclude
    require(sorted(watch) == pre['identity']['watch'] and sorted(exclude) == pre['identity']['exclude'],
            'Restore observation policy must equal the manifests')
    require(not pre['unknowns'] and not post['unknowns'], 'Manifests with unknown inputs cannot drive restore')
    current = capture(root, watch, exclude)
    result = {'mode': 'apply' if apply else 'dry-run', 'workspace': str(root), 'observed_at': now(),
              'pre_candidate': pre['candidate'], 'post_candidate': post['candidate'],
              'current_candidate': current['candidate'], 'unknowns': current['unknowns']}
    if current['unknowns']:
        return {**result, 'status': 'blocked', 'delta': [], 'actions': [], 'conflicts': [
            {'path': '', 'reason': 'current workspace has unknown inputs'}]}
    delta, index_delta, conflicts = restore_plan(pre, post, current)
    pf = pre['identity']['files']
    actions = []
    for name in delta:
        kind = pf.get(name, DELETED)['kind']
        actions.append({'path': name, 'action': 'remove' if kind == 'deleted' else 'write_' + kind})
    actions.extend({'path': name, 'action': 'restore_index'} for name in index_delta)
    result.update(delta=delta, index_delta=index_delta, actions=actions, conflicts=conflicts)
    if conflicts:
        return {**result, 'status': 'blocked'}
    if not actions:
        return {**result, 'status': 'nothing_to_restore', 'matches_pre_delta': True}
    if not apply:
        return {**result, 'status': 'restorable'}
    blobs = pre['blobs']
    for name in delta:
        path = safe_path(root, name)
        record = pf.get(name, DELETED)
        if record['kind'] == 'deleted':
            if path.is_symlink() or path.exists():
                path.unlink()
            for parent in path.parents:
                if parent == root or any(parent.iterdir()):
                    break
                parent.rmdir()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.restore-')
        os.close(fd); os.unlink(temporary)
        if record['kind'] == 'symlink':
            os.symlink(record['target'], temporary)
        else:
            Path(temporary).write_bytes(base64.b64decode(blobs[record['sha256']]))
            os.chmod(temporary, record['mode'])
        os.replace(temporary, path)
    pi = {e['path']: e for e in pre['identity']['index'] if e['stage'] == '0'}
    for name in index_delta:
        git(root, 'update-index', '--force-remove', '--', name)
        if name in pi:
            entry = pi[name]
            oid = entry['oid']
            if entry['mode'] != '160000' and subprocess.run(
                    ['git', '-C', str(root), 'cat-file', '-e', oid], capture_output=True).returncode:
                oid = subprocess.run(['git', '-C', str(root), 'hash-object', '-w', '--stdin'],
                                     input=base64.b64decode(blobs[entry['sha256']]), capture_output=True,
                                     check=True).stdout.decode().strip()
            git(root, 'update-index', '--add', '--cacheinfo', entry['mode'], oid, name)
    after = capture(root, watch, exclude)
    af = after['identity']['files']
    ai = {(e['path'], e['stage']): e for e in after['identity']['index']}
    pix = {(e['path'], e['stage']): e for e in pre['identity']['index']}
    verified = all(af.get(n, DELETED) == pf.get(n, DELETED) for n in delta) and \
        all(ai.get((n, '0')) == pix.get((n, '0')) for n in index_delta)
    return {**result, 'status': 'restored' if verified else 'blocked', 'matches_pre_delta': verified,
            'restored_candidate': after['candidate'], 'restored_unknowns': after['unknowns']}


# Operational state graph is shared with the explicit legacy converter.
TRANSITIONS = {'reserved': {'starting', 'cancelled'}, 'starting': {'running', 'unknown'},
               'unknown': {'running', 'cancelled', 'finished'}, 'running': {'finished'}}

# Ledger storage and command boundary. Nonledger helpers keep their own exit policy.
from contextlib import contextmanager

LEDGER_TYPE = 'workflow-invocation-journal'

ERROR_ACTIONS = {
    'file_not_found': 'locate_ledger', 'file_unreadable': 'fix_file_access',
    'invalid_json': 'repair_json_preserving_original', 'invalid_request': 'fix_request',
    'conversion_required': 'convert_ledger', 'unsupported_record_type': 'inspect_supported_format',
    'unsupported_schema_version': 'use_supported_version', 'invalid_ledger_shape': 'repair_ledger_preserving_original',
    'invalid_event_history': 'repair_history_preserving_original', 'invalid_event_transition': 'reconcile_host',
    'stale_revision': 'read_and_reconsider', 'event_id_conflict': 'use_original_event_or_new_id',
    'capacity_unknown': 'resolve_capacity', 'capacity_exhausted': 'review_limits',
    'manual_reconciliation_required': 'supply_mapping_or_opening', 'source_changed': 'stop_writers_and_recheck',
    'destination_exists': 'read_existing_destination', 'write_failed': 'read_and_reconcile',
    'output_failed': 'read_committed_result',
}


class LedgerError(ValueError):
    def __init__(self, code, json_path='$', reason=None, event_index=None, phase='preflight'):
        super().__init__(code)
        self.code = code
        self.json_path = json_path
        self.reason = reason or code
        self.event_index = event_index
        self.phase = phase
        self.mutation = {'ledger': 'none', 'artifacts': 'none'}
        self.revision = None
        self.event_id = None
        self.expected_type = None
        self.missing_keys = []
        self.unexpected_key_count = 0
        self.detail = None


def ledger_require(condition, code='invalid_ledger_shape', json_path='$', reason=None, detail=None):
    if not condition:
        error = LedgerError(code, json_path=json_path, reason=reason)
        error.detail = detail  # short, payload-free hint: only validated IDs, bucket names and fixed words
        raise error



def ledger_type(value, expected, json_path='$', code='invalid_ledger_shape'):
    kinds = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    if type(value) is not kinds[expected]:
        error = LedgerError(code, json_path=json_path, reason='wrong_type')
        error.expected_type = expected
        raise error


def ledger_fields(value, required, allowed=None, json_path='$', code='invalid_ledger_shape'):
    ledger_type(value, 'object', json_path, code)
    required = set(required)
    allowed = required if allowed is None else set(allowed)
    missing, extra = required - value.keys(), value.keys() - allowed
    if missing or extra:
        error = LedgerError(code, json_path=json_path, reason='field_mismatch')
        error.expected_type = 'object'
        error.missing_keys = sorted(missing)
        error.unexpected_key_count = len(extra)
        raise error
    try:
        ledger_field_types(value, allowed, json_path)
    except LedgerError as error:
        error.code = code
        raise

def ledger_field_types(value, schema, json_path):
    strings_required = {'id', 'run_id', 'kind', 'at', 'bucket', 'origin', 'authority', 'reason',
        'decision_owner', 'invocation', 'state', 'evidence', 'check_id', 'path', 'sha256',
        'unresolved_id', 'target', 'source', 'source_pointer', 'converter_version', 'converted_at',
        'actor', 'detected_format', 'preserved_path', 'history_level', 'request_identity',
        'relation', 'status', 'old_id', 'new_id', 'destination', 'mode', 'strategy',
        'preservation_directory', 'expected_sha256', 'view'}
    strings_nullable = {'role', 'unit', 'incident', 'purpose', 'model', 'effort', 'workspace',
                        'observed_model', 'observed_effort', 'outcome', 'not_started_evidence'}
    integers = {'schema_version', 'revision', 'expected_revision', 'remaining_work_estimate',
                'old_revision', 'new_revision'}
    nullable_integers = {'reported_used', 'baseline_used', 'materialized_started'}
    arrays = {'events', 'unresolved', 'resolutions', 'evidence_refs', 'sources', 'field_mappings',
              'unmapped', 'source_relationships', 'event_mappings'}
    objects = {'data', 'event', 'limits', 'opening_used', 'calls', 'checks', 'accounting',
               'event_metadata', 'quiescence', 'mapping', 'observed'}
    nullable_objects = {'expected_previous', 'scope_basis', 'provenance', 'opening'}
    for key in schema & value.keys():
        item = value[key]
        expected = ('string' if key in strings_required or key in strings_nullable else
                    'integer' if key in integers or key in nullable_integers or (key == 'value' and 'unresolved_id' not in schema) else
                    'array' if key in arrays or key == 'buckets' else
                    'object' if key in objects or key in nullable_objects else
                    'boolean' if key in {'started', 'writer'} else None)
        nullable = key in strings_nullable | nullable_integers | nullable_objects | {'buckets', 'started', 'writer'}
        if expected is not None and not (nullable and item is None):
            ledger_type(item, expected, json_path + '.' + key)



def ledger_path(value):
    # Paths are references, never URLs or arbitrary payloads reflected in diagnostics.
    ledger_require(isinstance(value, (str, Path)), 'invalid_request')
    value = str(value)
    ledger_require(bool(value.strip()) and not any(c in value for c in ('\x00', '\n', '\r', '://', '?', '#')),
                   'invalid_request')
    return Path(value)


def ledger_source(path):
    path = ledger_path(path)
    try:
        with regular_stream(path) as stream:
            info = os.fstat(stream.fileno())
            raw = stream.read()
        return raw, (info.st_dev, info.st_ino)
    except FileNotFoundError:
        raise LedgerError('file_not_found') from None
    except (OSError, ValueError):
        raise LedgerError('file_unreadable') from None


def ledger_decode(raw):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('nonfinite')
    try:
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise LedgerError('invalid_json') from None


SUMMARY_TYPES = {'run_id': str, 'revision': int, 'buckets': dict, 'active_calls': dict,
                 'unresolved': list, 'usage_known': bool, 'next_action': str}
LedgerFormat = namedtuple('LedgerFormat', 'name candidate')


def ledger_format_info(value):
    if not isinstance(value, dict):
        return LedgerFormat('unknown', 'unknown')
    if 'record_type' in value:
        return LedgerFormat(LEDGER_TYPE if value['record_type'] == LEDGER_TYPE else 'unknown', 'typed')
    if 'schema_version' in value and 'events' in value:
        return LedgerFormat('untyped-journal', 'legacy')
    if all(key in value and type(value[key]) is kind for key, kind in SUMMARY_TYPES.items()):
        return LedgerFormat('auxiliary-summary-candidate', 'result')
    if {'limits', 'calls'} <= value.keys() or {'used', 'limit'} <= value.keys():
        return LedgerFormat('legacy-snapshot-candidate', 'legacy')
    if any(k in value for k in ('total_calls', 'oracle_calls', 'total_used', 'summary', 'cumulative_total')):
        return LedgerFormat('auxiliary-summary-candidate', 'legacy')
    return LedgerFormat('unknown', 'unknown')


def ledger_classify(value):
    return ledger_format_info(value).name


def ledger_read(path):
    raw, _ = ledger_source(path)
    value = ledger_decode(raw)
    return value, ledger_replay(value)


def ledger_inspect(path):
    raw, _ = ledger_source(path)
    value = ledger_decode(raw)
    kind = ledger_classify(value)
    supported = (isinstance(value, dict) and value.get('record_type') == LEDGER_TYPE and
                 type(value.get('schema_version')) is int and value['schema_version'] == 1)
    try:
        state = ledger_replay(value)
        valid, known, action, diagnostic = True, state['usage_known'], state['next_action'], None
    except LedgerError as exc:
        valid, known, action, diagnostic = False, False, ERROR_ACTIONS[exc.code], exc.code
    return {'detected_format': kind, 'recognized': kind != 'unknown', 'supported': supported,
            'valid': valid, 'usage_known': known, 'next_action': action, 'diagnostic': diagnostic,
            'mutation': {'ledger': 'none', 'artifacts': 'none'}}


def ledger_destination(path):
    path = ledger_path(path)
    ledger_require(not path.exists() and not path.is_symlink(), 'destination_exists')
    ledger_require(path.parent.is_dir(), 'file_unreadable')
    return path


@contextmanager
def ledger_lock(path):
    lock = ledger_lock_path(path)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise LedgerError('file_unreadable')
        with os.fdopen(fd, 'r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield
    except LedgerError as exc:
        exc.mutation['artifacts'] = 'created'
        raise
    except OSError:
        exc = LedgerError('write_failed', phase='lock')
        exc.mutation['artifacts'] = 'unknown'
        raise exc from None


def ledger_publish(path, raw, replace=False):
    """Durable complete bytes; publication is the only commit point."""
    path = ledger_path(path)
    temporary = None
    published = False
    try:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.workflow-ledger-tmp-')
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            ledger_source(path)  # Refuse special-file replacement at commit.
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        published = True
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        raise LedgerError('destination_exists', phase='publish') from None
    except (OSError, LedgerError):
        exc = LedgerError('write_failed', phase='fsync' if published else 'write')
        exc.mutation = {'ledger': 'unknown' if published else 'none', 'artifacts': 'created'}
        raise exc from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                # Preserve the primary outcome; this owned temporary artifact may remain.
                pass


def ledger_init(path, run_id, limits):
    ledger_require(lid(run_id), 'invalid_request', '$.run_id')
    ledger_require(isinstance(limits, dict), 'invalid_request', '$.limits')
    opening = {'limits': limits, 'opening_used': {b: 0 for b in limits}, 'calls': {},
               'checks': {}, 'provenance': None, 'unresolved': []}
    value = {'record_type': LEDGER_TYPE, 'schema_version': 1, 'run_id': run_id,
             'events': [{'id': 'opening', 'kind': 'opening', 'data': opening, 'revision': 1, 'at': now()}]}
    state = ledger_replay(value)
    path = ledger_destination(path)
    with ledger_lock(path):
        ledger_destination(path)
        ledger_publish(path, encoded(value))
    return {**ledger_summary(state), 'mutation': {'ledger': 'committed', 'artifacts': 'created'}}


LEDGER_BATCH_LIMIT = 50


def ledger_update(path, expected_revision, event=None, events=None):
    """Apply one event or an ordered batch. A batch is validated, applied and published atomically."""
    ledger_require(type(expected_revision) is int and expected_revision >= 1, 'invalid_request', '$.expected_revision')
    ledger_require((event is None) != (events is None), 'invalid_request')
    batch = [event] if events is None else events
    ledger_require(isinstance(batch, list) and 0 < len(batch) <= LEDGER_BATCH_LIMIT, 'invalid_request')
    for item in batch:
        ledger_validate_request_event(item)
    ledger_require(len({item['id'] for item in batch}) == len(batch), 'invalid_request')
    path = ledger_path(path)
    ledger_read(path)  # Full preflight before the first lock artifact.
    with ledger_lock(path):
        value, state = ledger_read(path)
        known = {old['id']: old for old in value['events']}
        present = [item['id'] in known for item in batch]
        if any(present):
            # Identical retry succeeds even with a stale revision; a partial or changed batch fails.
            ledger_require(all(present) and all(all(known[item['id']][k] == item[k] for k in item) for item in batch),
                           'event_id_conflict')
            return {**ledger_summary(state), 'mutation': {'ledger': 'none', 'artifacts': 'created'}}
        ledger_require(expected_revision == state['revision'], 'stale_revision')
        fresh = []
        for offset, item in enumerate(batch):
            entry = {**item, 'revision': expected_revision + 1 + offset, 'at': now()}
            try:
                ledger_apply(state, entry)
            except LedgerError as exc:
                exc.event_index = len(value['events']) + offset
                prefix = '$.event' if events is None else f'$.events[{offset}]'
                exc.json_path = prefix + exc.json_path[1:]
                raise
            fresh.append(entry)
        value['events'].extend(fresh)
        # Apply validates semantics; replay also validates the complete serialized boundary.
        state = ledger_replay(value)
        try:
            ledger_publish(path, encoded(value), replace=True)
        except LedgerError as exc:
            exc.revision, exc.event_id = fresh[-1]['revision'], fresh[-1]['id']
            raise
    return {**ledger_summary(state), 'mutation': {'ledger': 'committed', 'artifacts': 'created'}}


def reconcile(path, observed):
    ledger_type(observed, 'object', '$.observed', 'invalid_request')
    for actual in observed.values():
        ledger_type(actual, 'string', '$.observed', 'invalid_request')
    ledger_require(isinstance(observed, dict) and all(lid(k) and isinstance(v, str) and
                   v in {'reserved', 'starting', 'running', 'unknown', 'finished', 'cancelled'}
                   for k, v in observed.items()), 'invalid_request', '$.observed')
    _, state = ledger_read(path)
    mismatches = [{'invocation': name, 'recorded': call['state'], 'observed': observed.get(name, 'unknown')}
                  for name, call in state['calls'].items() if observed.get(name) != call['state']]
    unrecorded = sorted(set(observed) - state['calls'].keys())
    return {'run_id': state['run_id'], 'revision': state['revision'], 'usage': state['usage'],
            'usage_known': state['usage_known'], 'mismatches': mismatches, 'unrecorded': unrecorded,
            'next_action': 'reconcile_host' if mismatches or unrecorded else state['next_action'],
            'mutation': {'ledger': 'none', 'artifacts': 'none'}}


def ledger_snapshot(path, reference_path=None):
    raw, _ = ledger_source(path)
    summary = ledger_summary(ledger_replay(ledger_decode(raw)))
    return {'record_type': 'workflow-invocation-snapshot', 'schema_version': 1,
            'ledger': {'path': str(reference_path if reference_path is not None else path),
                       'sha256': hashlib.sha256(raw).hexdigest(), 'run_id': summary['run_id'],
                       'revision': summary['revision']},
            'limits_by_origin': {b: v['limits_by_origin'] for b, v in summary['buckets'].items()},
            'usage': summary['buckets'], 'active_calls': summary['active_calls'], 'unresolved': summary['unresolved']}


PathReference = namedtuple('PathReference', 'role path')


def command_path(value):
    require(isinstance(value, (str, Path)) and bool(str(value).strip()) and '\x00' not in str(value),
            'Invalid command path.')
    return Path(value)


def named_paths(data, *names):
    return [PathReference(name, command_path(data[name])) for name in names]


def journal_paths(data):
    path = command_path(data['path'])
    return [PathReference('journal', path), PathReference('lock', ledger_lock_path(path))]


def conversion_paths(data):
    require(isinstance(data['sources'], list), 'Invalid command paths.')
    result = named_paths(data, 'destination', 'preservation_directory')
    result.append(PathReference('lock', ledger_lock_path(data['destination'])))
    for source in data['sources']:
        require(isinstance(source, dict), 'Invalid command paths.')
        result.extend(named_paths(source, 'path'))
    return result


def gate_paths(data):
    require(isinstance(data['checks'], list), 'Invalid command paths.')
    return named_paths(data, 'requirements') + [
        PathReference('check', command_path(path)) for path in data['checks']]


def handoff_paths(data):
    root = command_path(data['root'])
    draft = data['draft']
    require(isinstance(draft, dict), 'Invalid command paths.')
    def project_path(value):
        path = command_path(value)
        require(not path.is_absolute() and '..' not in path.parts, 'Invalid command paths.')
        # Identify first; the handler retains its root-containment/access checks.
        return root / path
    result = [PathReference('handoff', project_path(data['output']))]
    references, checks = draft.get('references', []), draft.get('checks', [])
    require(isinstance(references, list) and isinstance(checks, list), 'Invalid command paths.')
    paths = list(checks)
    for reference in references:
        require(isinstance(reference, dict), 'Invalid command paths.')
        paths.append(reference['path'])
    for key in ('ledger', 'previous'):
        if draft.get(key) is not None and draft.get(key) != '':
            paths.append(draft[key])
    result.extend(PathReference('reference', project_path(path)) for path in paths)
    return result


# Inventory only: request schemas and execution remain in their existing adapters.
ACTIONS = {
    'ledger-init': ('ledger', journal_paths),
    'ledger-inspect': ('ledger', journal_paths),
    'ledger-convert': ('ledger', conversion_paths),
    'ledger-update': ('ledger', journal_paths),
    'ledger-read': ('ledger', journal_paths),
    'reconcile': ('ledger', journal_paths),
    'snapshot': ('other', lambda data: []),
    'audit': ('other', lambda data: named_paths(data, 'before', 'after')),
    'restore': ('other', lambda data: named_paths(data, 'pre', 'post')),
    'plan': ('other', lambda data: named_paths(data, 'plan', 'requirements')),
    'check': ('other', lambda data: named_paths(data, 'requirements', 'log')),
    'gate': ('other', gate_paths),
    'bundle-digest': ('other', lambda data: []),
    'handoff': ('other', handoff_paths),
    'handoff-validate': ('other', lambda data: named_paths(data, 'directory')),
}
LEDGER_ACTIONS = {name for name, (group, _) in ACTIONS.items() if group == 'ledger'}


def ledger_request_stream(path):
    return str(path) == '/dev/stdin' or str(path).startswith(('/dev/fd/', '/proc/self/fd/'))


def collect_command_paths(action, data, input_path=None):
    require(isinstance(data, dict), 'Invalid command paths.')
    references = ACTIONS[action][1](data)
    if input_path is not None and not (action in LEDGER_ACTIONS and ledger_request_stream(input_path)):
        references.append(PathReference('request', command_path(input_path)))
    if action in LEDGER_ACTIONS:
        for reference in references:
            ledger_path(reference.path)
    return references


def result_output_issue(output, references, ledger=False):
    output = command_path(output)
    identity = inspect_path_identity(output)
    relations = [path_relation(identity, inspect_path_identity(ref.path)) for ref in references]
    if any(relation not in ('disjoint', 'undetermined') for relation in relations):
        return 'output_path_conflict'
    if ledger and output.name.startswith('.workflow-ledger-tmp-'):
        return 'reserved_output_name'
    if identity.uncertain or 'undetermined' in relations:
        return 'path_identity_unavailable'
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return 'path_identity_unavailable'
    else:
        return 'destination_exists'
    try:
        if ledger:
            if not output.parent.is_dir():
                return 'output_parent_unavailable'
        else:
            for parent in output.parents:
                if (parent.exists() or parent.is_symlink()) and not parent.is_dir():
                    return 'output_parent_unavailable'
    except OSError:
        return 'output_parent_unavailable'
    return None


def ledger_output_preflight(output, data, action, input_path=None):
    output = ledger_path(output)
    try:
        issue = result_output_issue(output, collect_command_paths(action, data, input_path), ledger=True)
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        raise LedgerError('invalid_request') from None
    errors = {
        'output_path_conflict': ('invalid_request', 'output_path_conflict'),
        'reserved_output_name': ('invalid_request', 'invalid_request'),
        'path_identity_unavailable': ('file_unreadable', 'path_identity_unavailable'),
        'destination_exists': ('destination_exists', 'destination_exists'),
        'output_parent_unavailable': ('file_unreadable', 'file_unreadable'),
    }
    if issue:
        code, reason = errors[issue]
        raise LedgerError(code, reason=reason)
    return output


def command_output_preflight(output, data, action, input_path):
    try:
        issue = result_output_issue(output, collect_command_paths(action, data, input_path))
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        raise ValueError('Invalid command paths.') from None
    messages = {
        'output_path_conflict': 'Result output conflicts with a command path.',
        'path_identity_unavailable': 'Cannot establish result output path separation.',
        'destination_exists': 'Output must be a new file.',
        'output_parent_unavailable': 'Output parent must be a directory.',
    }
    if issue:
        raise ValueError(messages[issue])


def validate_request_event_at(event, json_path, index=None):
    """Validate one request event; errors name the event (and inner field) instead of just `$`."""
    try:
        ledger_validate_request_event(event)
    except LedgerError as exc:
        exc.json_path = json_path + exc.json_path[1:]
        exc.event_index = index
        raise


def ledger_request(action, data):
    required = {
        'ledger-init': {'path', 'run_id', 'limits'}, 'ledger-read': {'path'},
        'ledger-inspect': {'path'}, 'ledger-update': {'path', 'expected_revision', 'event'},
        'reconcile': {'path', 'observed'},
        'ledger-convert': {'sources', 'destination', 'run_id', 'mode', 'strategy', 'mapping', 'preservation_directory'}}
    allowed = required[action] | ({'view'} if action == 'ledger-read' else set())
    if action == 'ledger-update' and isinstance(data, dict) and 'events' in data:
        required = {**required, action: {'path', 'expected_revision', 'events'}}  # batch form replaces `event`
        allowed = required[action]
    ledger_fields(data, required[action], allowed, code='invalid_request')
    if action == 'ledger-read':
        ledger_require(data.get('view', 'summary') in ('summary', 'full'), 'invalid_request')
    if 'path' in data:
        ledger_path(data['path'])
    if action == 'ledger-update':
        ledger_require(type(data['expected_revision']) is int and data['expected_revision'] >= 1, 'invalid_request')
        if 'events' in data:
            events = data['events']
            ledger_require(isinstance(events, list) and 0 < len(events) <= LEDGER_BATCH_LIMIT, 'invalid_request')
            for index, event in enumerate(events):
                validate_request_event_at(event, f'$.events[{index}]', index)
            ledger_require(len({event['id'] for event in events}) == len(events), 'invalid_request')
        else:
            validate_request_event_at(data['event'], '$.event')
    if action == 'ledger-convert':
        ledger_require(isinstance(data['sources'], list) and data['sources'], 'invalid_request')
        for source in data['sources']:
            ledger_fields(source, {'path', 'expected_sha256'}, code='invalid_request')
            ledger_path(source['path'])
            ledger_require(sha256(source['expected_sha256']), 'invalid_request')
        ledger_path(data['destination']); ledger_path(data['preservation_directory'])


def ledger_error_json(exc, action, data):
    path = data.get('path', data.get('destination')) if isinstance(data, dict) else None
    try:
        path = str(ledger_path(path)) if path is not None else None
    except LedgerError:
        path = None
    messages = {
        'output_path_conflict': 'Result output conflicts with a command path. The request identifies the journal; use a fresh result path or omit --output.',
        'result_is_not_journal': 'This result summary is not a journal and has no event history. Preserve the original and reconstruct from evidence.',
        'path_identity_unavailable': 'Cannot establish result output path separation.',
    }
    message = messages.get(exc.reason, ERROR_ACTIONS[exc.code].replace('_', ' ') + '.')
    return {'error': {'code': exc.code, 'action': action, 'message': message,
            'path': path, 'json_path': exc.json_path, 'expected_record_type': LEDGER_TYPE,
            'supported_schema_versions': [1], 'detected_format': getattr(exc, 'detected_format', None),
            'usage_known': False, 'mutation': exc.mutation, 'next_action': ERROR_ACTIONS[exc.code],
            'event_index': exc.event_index, 'reason': exc.reason, 'phase': exc.phase,
            'revision': exc.revision, 'event_id': exc.event_id, 'expected_type': exc.expected_type,
            'missing_keys': exc.missing_keys, 'unexpected_key_count': exc.unexpected_key_count,
            **({'detail': exc.detail} if exc.detail else {})}}


def request_event_id(data):
    batch = data.get('events')
    event = batch[-1] if isinstance(batch, list) and batch else data.get('event')
    return event.get('id') if isinstance(event, dict) else None


def ledger_main(action, input_path, output=None, request=None, collect=None):
    """Run one ledger action. `request` is an already decoded inline request (batch steps);
    `collect` receives the result/error instead of stdout/stderr."""
    data = None
    result = None
    def emit_error(exc):
        payload = ledger_error_json(exc, action, data)
        if collect is None:
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        else:
            collect['error'] = payload
    try:
        if request is not None:
            data = request
        else:
            # CLI requests may be /dev/stdin or process substitution streams.
            try:
                request_path = Path(input_path)
                is_stream = ledger_request_stream(request_path)
                raw = request_path.read_bytes() if is_stream else ledger_source(request_path)[0]
                data = ledger_decode(raw)
            except OSError:
                raise LedgerError('file_unreadable') from None
        ledger_request(action, data)
        if output is not None:
            ledger_output_preflight(output, data, action, input_path)
        if action == 'ledger-read':
            _, state = ledger_read(data['path'])
            result = state if data.get('view', 'summary') == 'full' else ledger_summary(state)
            result = {**result, 'mutation': {'ledger': 'none', 'artifacts': 'none'}}
        elif action == 'ledger-init':
            result = ledger_init(**data)
        elif action == 'ledger-update':
            result = ledger_update(**data)
        elif action == 'ledger-inspect':
            result = ledger_inspect(**data)
        elif action == 'reconcile':
            result = reconcile(**data)
        else:
            result = ledger_convert(**data)
        if output is not None:
            result['mutation']['artifacts'] = 'created'
            try:
                ledger_publish(output, encoded(result))
            except LedgerError as failure:
                exc = LedgerError('output_failed', phase='output')
                exc.mutation = dict(result['mutation'])
                exc.mutation['artifacts'] = 'unknown' if failure.mutation['ledger'] == 'unknown' else 'created'
                exc.revision = result.get('revision')
                exc.event_id = request_event_id(data)
                raise exc from None
        if collect is not None:
            collect['result'] = result
        else:
            try:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            except OSError:
                exc = LedgerError('output_failed', phase='output')
                exc.mutation = dict(result['mutation'])
                exc.revision = result.get('revision')
                exc.event_id = request_event_id(data)
                raise exc from None
        if action == 'reconcile':
            return int(bool(result['mismatches'] or result['unrecorded'] or not result['usage_known']))
        if action == 'ledger-convert':
            return int(bool(result.get('unresolved')) or not result['usage_known'])
        return 0
    except LedgerError as exc:
        emit_error(exc)
        return 2
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        # Defensive boundary: raw payloads and OS messages must never escape to stderr.
        exc = LedgerError('invalid_request')
        if result is not None:
            exc.mutation = result['mutation']
        emit_error(exc)
        return 2

# Typed ledger state engine. Counters exist only during replay, never on disk.
import copy
import re
LEDGER_ORIGINS = ('operating', 'user', 'host')
LEDGER_RESERVE_FIELDS = {'invocation', 'role', 'unit', 'incident', 'purpose', 'model', 'effort', 'buckets', 'writer', 'workspace'}
LEDGER_CALL_FIELDS = LEDGER_RESERVE_FIELDS | {'state', 'started', 'outcome', 'observed_model', 'observed_effort', 'evidence_refs'}
LEDGER_OPENING_FIELDS = {'limits', 'opening_used', 'calls', 'checks', 'provenance', 'unresolved'}


def lid(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,95}', value) is not None


def lcount(value):
    return type(value) is int and value >= 0


def lbucket(value):
    ledger_type(value, 'string')
    return value == 'total' or (isinstance(value, str) and ':' in value and
        value.split(':', 1)[0] in {'role', 'unit', 'incident'} and lid(value.split(':', 1)[1]))


def lfields(value, fields, path='$'):
    ledger_fields(value, fields, json_path=path)


def lrefs(value, required=False):
    ledger_type(value, 'array')
    for item in value:
        ledger_type(item, 'string')
    return (bool(value) or not required) and all(nonempty(x) for x in value)


def lpointer(*parts):
    return '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in parts)


def lparts(value):
    ledger_require(isinstance(value, str) and value.startswith('/') and
                   re.search(r'~(?![01])', value) is None)
    return [p.replace('~1', '/').replace('~0', '~') for p in value[1:].split('/')]


def lentry(value):
    lfields(value, {'value', 'authority', 'reason'})
    ledger_type(value['value'], 'integer', '$.value')
    ledger_type(value['authority'], 'string', '$.authority')
    ledger_type(value['reason'], 'string', '$.reason')
    ledger_require(lcount(value['value']) and nonempty(value['authority']) and nonempty(value['reason']))


def lcheck(value):
    lfields(value, {'check_id', 'path', 'sha256'})
    ledger_require(lid(value['check_id']) and nonempty(value['path']) and sha256(value['sha256']))


def lcall(call, name, limits, imported=False):
    lfields(call, LEDGER_CALL_FIELDS)
    ledger_require(lid(name) and call['invocation'] == name)
    for field in ('role', 'purpose', 'model', 'effort', 'workspace', 'observed_model', 'observed_effort'):
        if not imported:
            ledger_type(call[field], 'string', '$.' + field)
        ledger_require((imported and call[field] is None) or nonempty(call[field]))
    ledger_require(call['role'] is None or lid(call['role']))
    for field in ('unit', 'incident'):
        ledger_require((imported and call[field] is None) or call[field] == '' or lid(call[field]))
    if not imported:
        ledger_type(call['writer'], 'boolean', '$.writer')
    ledger_require(type(call['writer']) is bool or (imported and call['writer'] is None))
    buckets = call['buckets']
    if buckets is None:
        ledger_require(imported)
    else:
        ledger_require(isinstance(buckets, list) and bool(buckets) and all(lbucket(b) for b in buckets)
                       and len(set(buckets)) == len(buckets) and set(buckets) <= set(limits))
        required = {'total'}
        for field in ('role', 'unit', 'incident'):
            if call[field]:
                required.add(field + ':' + call[field])
        # A non-empty role/unit/incident needs its own `role:<id>`/`unit:<id>`/`incident:<id>` bucket, and no others.
        ledger_require(required <= set(buckets), json_path='$.buckets', reason='missing_required_bucket')
        if all(call[k] is not None for k in ('role', 'unit', 'incident')):
            ledger_require(required == set(buckets), json_path='$.buckets', reason='unexpected_bucket')
    state, started = call['state'], call['started']
    ledger_require(isinstance(state, str) and state in {'reserved', 'starting', 'running', 'unknown', 'finished', 'cancelled'})
    ledger_require(started is None or type(started) is bool)
    ledger_require((state in {'reserved', 'cancelled'} and started is False) or
                   (state == 'starting' and started is None) or
                   (state in {'running', 'finished'} and started is True) or state == 'unknown')
    ledger_require((state == 'finished' and isinstance(call['outcome'], str) and
                    call['outcome'] in {'complete', 'partial', 'failed', 'interrupted'}) or
                   (state != 'finished' and call['outcome'] is None))
    ledger_require(lrefs(call['evidence_refs']))
    if state == 'unknown' and started is False:
        ledger_require(any(ref.startswith('not-started:') for ref in call['evidence_refs']))


def lprovenance(value):
    if value is None:
        return
    lfields(value, {'sources', 'converter_version', 'schema_version', 'converted_at', 'actor', 'authority',
                    'reason', 'field_mappings', 'unmapped', 'source_relationships', 'event_mappings',
                    'history_level', 'request_identity', 'quiescence', 'accounting', 'event_metadata'})
    ledger_require(value['converter_version'] == '1' and type(value['schema_version']) is int and
                   value['schema_version'] == 1 and timestamp(value['converted_at']) and sha256(value['request_identity']))
    ledger_require(all(nonempty(value[k]) for k in ('actor', 'authority', 'reason')) and
                   value['history_level'] in ('full-events', 'opening-summary'))
    lfields(value['quiescence'], {'status', 'evidence_refs'})
    ledger_require(value['quiescence']['status'] == 'quiescent' and lrefs(value['quiescence']['evidence_refs'], True))
    for field in ('sources', 'field_mappings', 'unmapped', 'source_relationships', 'event_mappings'):
        ledger_require(isinstance(value[field], list))
    ledger_require(bool(value['sources']))
    for source in value['sources']:
        lfields(source, {'path', 'sha256', 'detected_format', 'preserved_path'})
        ledger_require(all(nonempty(source[k]) for k in source) and sha256(source['sha256']))
    for item in value['field_mappings']:
        lfields(item, {'source', 'source_pointer', 'target', 'evidence_refs'})
        ledger_require(all(nonempty(item[k]) for k in ('source', 'target')) and
                       isinstance(item['source_pointer'], str) and lrefs(item['evidence_refs'], True))
    for item in value['unmapped']:
        lfields(item, {'source', 'source_pointer', 'reason'})
        ledger_require(nonempty(item['source']) and isinstance(item['source_pointer'], str) and nonempty(item['reason']))
    for item in value['source_relationships']:
        lfields(item, {'sources', 'relation', 'evidence_refs'})
        ledger_require(lrefs(item['sources'], True) and item['relation'] in ('independent', 'overlap', 'aggregate', 'unknown')
                       and lrefs(item['evidence_refs'], True))
    ledger_require(isinstance(value['accounting'], dict) and isinstance(value['event_metadata'], dict))
    for bucket, account in value['accounting'].items():
        ledger_require(lbucket(bucket))
        lfields(account, {'reported_used', 'baseline_used', 'materialized_started', 'evidence_refs'})
        ledger_require(all(account[k] is None or lcount(account[k]) for k in ('reported_used', 'baseline_used', 'materialized_started')) and lrefs(account['evidence_refs'], True))
    for old_id, metadata in value['event_metadata'].items():
        ledger_require(nonempty(old_id))
        lfields(metadata, {'decision_owner', 'remaining_work_estimate', 'scope_basis', 'evidence_refs'})
        ledger_require(nonempty(metadata['decision_owner']) and lcount(metadata['remaining_work_estimate']) and lrefs(metadata['evidence_refs'], True))
        if metadata['scope_basis'] is not None:
            lfields(metadata['scope_basis'], {'status', 'evidence_refs'})
            ledger_require(metadata['scope_basis']['status'] in ('known-empty', 'unknown') and lrefs(metadata['scope_basis']['evidence_refs'], True))
    for item in value['event_mappings']:
        lfields(item, {'source', 'old_id', 'old_revision', 'new_id', 'new_revision'})
        ledger_require(nonempty(item['source']) and nonempty(item['old_id']) and lid(item['new_id']) and
                       lcount(item['old_revision']) and item['old_revision'] > 0 and
                       lcount(item['new_revision']) and item['new_revision'] > 0)


def ledger_validate_opening(data, replay_state=False):
    lfields(data, LEDGER_OPENING_FIELDS)
    limits, baseline = data['limits'], data['opening_used']
    ledger_require(isinstance(limits, dict) and isinstance(baseline, dict) and set(limits) == set(baseline))
    for bucket, entries in limits.items():
        ledger_require(lbucket(bucket))
        ledger_fields(entries, set(), LEDGER_ORIGINS, '$.limits')
        for entry in entries.values():
            lentry(entry)
        if baseline[bucket] is not None:
            ledger_type(baseline[bucket], 'integer', '$.opening_used')
        ledger_require(baseline[bucket] is None or lcount(baseline[bucket]))
    ledger_require(isinstance(data['calls'], dict) and isinstance(data['checks'], dict))
    lprovenance(data['provenance'])
    for name, call in data['calls'].items():
        lcall(call, name, limits, imported=replay_state or data['provenance'] is not None)
    for name, check in data['checks'].items():
        lcheck(check)
        ledger_require(name == check['check_id'])
    ledger_require(isinstance(data['unresolved'], list))
    ids, targets = set(), set()
    for item in data['unresolved']:
        lfields(item, {'id', 'target', 'buckets', 'reason', 'evidence_refs'})
        ledger_require(lid(item['id']) and item['id'] not in ids and isinstance(item['target'], str) and item['target'] not in targets)
        ids.add(item['id']); targets.add(item['target'])
        ledger_require(item['reason'] in ('usage_unknown', 'started_unknown', 'membership_unknown',
                       'metadata_unknown', 'limit_origin_unknown', 'source_relation_unknown') and lrefs(item['evidence_refs'], True))
        affected = item['buckets']
        ledger_require(affected is None or (isinstance(affected, list) and bool(affected) and
                       all(lbucket(b) for b in affected) and len(set(affected)) == len(affected) and set(affected) <= set(limits)))
        parts = lparts(item['target'])
        if len(parts) == 2 and parts[0] == 'opening_used':
            ledger_require(parts[1] in baseline and baseline[parts[1]] is None and (affected is None or parts[1] in affected))
        elif len(parts) == 3 and parts[0] == 'calls':
            ledger_require(parts[1] in data['calls'] and parts[2] in LEDGER_CALL_FIELDS - {'outcome', 'state', 'invocation', 'evidence_refs'}
                           and data['calls'][parts[1]][parts[2]] is None)
            call_buckets = data['calls'][parts[1]]['buckets']
            ledger_require(affected is None or (call_buckets is not None and set(call_buckets) <= set(affected)))
            if parts[2] == 'buckets':
                ledger_require(affected is None)
        elif len(parts) == 3 and parts[0] == 'limits':
            ledger_require(parts[1] in limits and parts[2] in LEDGER_ORIGINS and parts[2] not in limits[parts[1]] and
                           item['reason'] == 'limit_origin_unknown' and affected is None)
        else:
            ledger_require(False)
    for bucket, amount in baseline.items():
        if amount is None:
            ledger_require(lpointer('opening_used', bucket) in targets)
        if not limits[bucket]:
            ledger_require(any(x['reason'] == 'limit_origin_unknown' and lparts(x['target'])[1] == bucket for x in data['unresolved']))
    for name, call in data['calls'].items():
        for field in LEDGER_CALL_FIELDS - {'outcome', 'state', 'invocation', 'evidence_refs'}:
            if call[field] is None and not replay_state:
                ledger_require(lpointer('calls', name, field) in targets)
    if data['provenance'] is None and not replay_state:
        ledger_require(not data['calls'] and not data['checks'] and not data['unresolved'] and
                       all(v == 0 for v in baseline.values()) and all(limits.values()))


def _lcount_call(state, call, sign):
    if call['buckets'] is None:
        state['_membership_unknown'] += sign
        return
    for bucket in call['buckets']:
        counts = state['_counts'].setdefault(bucket, [0, 0, 0])
        counts[0] += sign * (call['started'] is True)
        counts[1] += sign * (call['started'] is not True and call['state'] in {'reserved', 'starting', 'unknown'})
        counts[2] += sign * (call['started'] is None)


def _lcounters(state):
    state['_counts'] = {b: [0, 0, 0] for b in state['limits']}
    state['_membership_unknown'] = 0
    for call in state['calls'].values():
        _lcount_call(state, call, 1)


def _lusage(state, bucket):
    started, reserved, unknown = state['_counts'].get(bucket, (0, 0, 0))
    baseline, origins = state['opening_used'][bucket], state['limits'][bucket]
    known = baseline is not None and not unknown and not state['_membership_unknown']
    used = baseline + started if known else None
    limit = min((v['value'] for v in origins.values()), default=None)
    return {'limits_by_origin': copy.deepcopy(origins), 'effective_limit': limit,
            'binding_origins': [o for o in LEDGER_ORIGINS if o in origins and origins[o]['value'] == limit],
            'used': used, 'reserved': reserved, 'available': limit - used - reserved if known and limit is not None else None,
            'usage_known': bool(known)}


def _lrefresh(state):
    state['usage'] = {b: _lusage(state, b) for b in state['limits']}
    state['usage_known'] = all(u['usage_known'] for u in state['usage'].values())
    state['next_action'] = ('resolve_import' if state['unresolved'] else 'reconcile_host' if not state['usage_known'] else
                            'review_limits' if any(u['available'] is None or u['available'] <= 0 for u in state['usage'].values()) else 'continue')


def ledger_summary(state):
    return {'run_id': state['run_id'], 'revision': state['revision'], 'buckets': copy.deepcopy(state['usage']),
            'active_calls': {name: {k: copy.deepcopy(call[k]) for k in ('state', 'started', 'buckets')}
                             for name, call in state['calls'].items() if call['state'] not in {'finished', 'cancelled'}},
            'unresolved': [{k: copy.deepcopy(item[k]) for k in ('id', 'reason', 'buckets')} | {'next_action': 'resolve_import'}
                           for item in state['unresolved']], 'usage_known': state['usage_known'], 'next_action': state['next_action']}


def _ledger_validate_request_event(event):
    lfields(event, {'id', 'kind', 'data'})
    ledger_require(lid(event['id']) and isinstance(event['kind'], str) and
                   event['kind'] in {'limit', 'reserve', 'transition', 'check', 'resolve-import'}, code='invalid_request')
    data = event['data']
    fields = {'limit': {'bucket', 'origin', 'value', 'authority', 'reason', 'decision_owner', 'remaining_work_estimate', 'expected_previous', 'scope_basis'},
              'reserve': LEDGER_RESERVE_FIELDS,
              'transition': {'invocation', 'state', 'evidence', 'outcome', 'observed_model', 'observed_effort', 'not_started_evidence'},
              'check': {'check_id', 'path', 'sha256'}, 'resolve-import': {'resolutions', 'authority', 'evidence_refs'}}
    lfields(data, fields[event['kind']])
    if event['kind'] == 'limit':
        ledger_require(lbucket(data['bucket']) and data['origin'] in LEDGER_ORIGINS)
        lentry({k: data[k] for k in ('value', 'authority', 'reason')})
        ledger_require(nonempty(data['decision_owner']) and lcount(data['remaining_work_estimate']))
        if data['expected_previous'] is not None:
            lentry(data['expected_previous'])
        if data['scope_basis'] is not None:
            lfields(data['scope_basis'], {'status', 'evidence_refs'})
            ledger_require(data['scope_basis']['status'] in ('known-empty', 'unknown') and lrefs(data['scope_basis']['evidence_refs'], True))
    elif event['kind'] == 'reserve':
        ledger_require(lid(data['invocation']) and isinstance(data['buckets'], list) and all(lbucket(b) for b in data['buckets']))
        lcall({**data, 'state': 'reserved', 'started': False, 'outcome': None, 'observed_model': 'unverified',
               'observed_effort': 'unverified', 'evidence_refs': []}, data['invocation'], {b: {} for b in data['buckets']})
    elif event['kind'] == 'transition':
        ledger_require(lid(data['invocation']), json_path='$.data.invocation', reason='invalid_invocation_id',
                       detail='invocation must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$')
        ledger_require(data['state'] in ('starting', 'running', 'unknown', 'finished', 'cancelled'), json_path='$.data.state',
                       reason='invalid_state', detail='state must be starting, running, unknown, finished or cancelled')
        for name in ('evidence', 'observed_model', 'observed_effort'):
            ledger_require(nonempty(data[name]), json_path='$.data.' + name, reason='empty_' + name,
                           detail=name + ' must be a nonempty string (observed_* may be "unverified")')
        ledger_require(data['not_started_evidence'] is None or nonempty(data['not_started_evidence']),
                       json_path='$.data.not_started_evidence', reason='empty_not_started_evidence',
                       detail='not_started_evidence must be null or a nonempty string')
        ledger_require((data['state'] == 'finished' and data['outcome'] in ('complete', 'partial', 'failed', 'interrupted')) or
                       (data['state'] != 'finished' and data['outcome'] is None), json_path='$.data.outcome',
                       reason='invalid_outcome',
                       detail='outcome must be complete, partial, failed or interrupted when state is finished, otherwise null '
                              '(worker result names such as candidate_ready belong in evidence)')
    elif event['kind'] == 'check':
        lcheck(data)
    else:
        ledger_require(isinstance(data['resolutions'], list) and bool(data['resolutions']) and nonempty(data['authority']) and lrefs(data['evidence_refs'], True))
        for patch in data['resolutions']:
            lfields(patch, {'unresolved_id', 'target', 'expected_value', 'value'})
            ledger_require(lid(patch['unresolved_id']))
            lparts(patch['target'])


def ledger_validate_request_event(event):
    try:
        _ledger_validate_request_event(event)
    except LedgerError as error:
        error.code = 'invalid_request'
        raise


def _ledger_apply(state, event):
    ledger_validate_request_event({k: event[k] for k in ('id', 'kind', 'data')})
    owned_counters = '_counts' not in state
    if owned_counters:
        _lcounters(state)
    kind, data = event['kind'], event['data']
    if kind == 'limit':
        bucket, origin = data['bucket'], data['origin']
        existing = bucket in state['limits']
        old = state['limits'].get(bucket, {}).get(origin)
        ledger_require(old == data['expected_previous'], code='invalid_event_transition')
        ledger_require(old is None or origin == 'operating', code='invalid_event_transition')
        ledger_require((existing and data['scope_basis'] is None) or (not existing and data['scope_basis'] is not None))
        if old is not None:
            capacity = _lusage(state, bucket)
            ledger_require(capacity['usage_known'], code='capacity_unknown')
            ledger_require(data['value'] >= capacity['used'] + capacity['reserved'], code='capacity_exhausted')
        if not existing:
            basis = data['scope_basis']
            state['limits'][bucket] = {}
            state['opening_used'][bucket] = 0 if basis['status'] == 'known-empty' else None
            state['_counts'][bucket] = [0, 0, 0]
            if basis['status'] == 'unknown':
                state['unresolved'].append({'id': 'scope:' + hashlib.sha256(event['id'].encode()).hexdigest()[:32],
                    'target': lpointer('opening_used', bucket), 'buckets': [bucket], 'reason': 'usage_unknown',
                    'evidence_refs': copy.deepcopy(basis['evidence_refs'])})
        state['limits'][bucket][origin] = {k: data[k] for k in ('value', 'authority', 'reason')}
    elif kind == 'reserve':
        ledger_require(data['invocation'] not in state['calls'], code='invalid_event_transition', json_path='$.data.invocation',
                       reason='invocation_exists', detail=f'invocation {data["invocation"]} is already reserved; use a new invocation ID')
        for bucket in data['buckets']:
            where = '$.data.buckets[' + bucket + ']'
            ledger_require(bucket in state['limits'], code='capacity_unknown', json_path=where, reason='bucket_without_limit',
                           detail=f'bucket {bucket} has no limit; add it to ledger-init limits or a limit event first')
            capacity = _lusage(state, bucket)
            starting = sorted(name for name, call in state['calls'].items()
                              if call['started'] is None and call['state'] in {'starting', 'unknown'} and
                              (call['buckets'] is None or bucket in call['buckets']))
            ledger_require(capacity['usage_known'], code='capacity_unknown', json_path=where, reason='usage_unknown',
                           detail=(f'usage of bucket {bucket} is unknown while invocation(s) {", ".join(starting)} have an unobserved start '
                                   '(state starting/unknown). Record running/finished/cancelled for them first, or reserve every '
                                   'invocation before recording any starting') if starting else
                                  f'usage of bucket {bucket} is unknown (null baseline); resolve the imported unknown first')
            ledger_require(capacity['effective_limit'] is not None and
                           not any(x['buckets'] is None or bucket in x['buckets'] for x in state['unresolved']),
                           code='capacity_unknown', json_path=where, reason='unresolved_blocks_bucket',
                           detail=f'bucket {bucket} has no effective limit or a relevant unresolved fact; resolve it first')
            ledger_require(capacity['available'] > 0, code='capacity_exhausted', json_path=where, reason='no_available_capacity',
                           detail=f'bucket {bucket}: used {capacity["used"]} + reserved {capacity["reserved"]} '
                                  f'has reached limit {capacity["effective_limit"]}')
        call = {**copy.deepcopy(data), 'state': 'reserved', 'started': False, 'outcome': None,
                'observed_model': 'unverified', 'observed_effort': 'unverified', 'evidence_refs': []}
        state['calls'][data['invocation']] = call
        _lcount_call(state, call, 1)
    elif kind == 'transition':
        ledger_require(data['invocation'] in state['calls'], code='invalid_event_transition', json_path='$.data.invocation',
                       reason='unknown_invocation', detail=f'invocation {data["invocation"]} has no reserve event')
        old = state['calls'][data['invocation']]
        ledger_require(data['state'] in TRANSITIONS.get(old['state'], set()), code='invalid_event_transition',
                       json_path='$.data.state', reason='transition_not_allowed',
                       detail=f'{data["invocation"]} is {old["state"]}; allowed next: {", ".join(sorted(TRANSITIONS.get(old["state"], set()))) or "none (terminal)"}')
        if data['state'] == 'cancelled' and old['state'] == 'unknown':
            ledger_require(old['started'] is not True and nonempty(data['not_started_evidence']), code='invalid_event_transition')
        call = copy.deepcopy(old)
        call.update({k: data[k] for k in ('state', 'outcome', 'observed_model', 'observed_effort')})
        call['started'] = (True if data['state'] in {'running', 'finished'} else False if data['state'] == 'cancelled' else
                           None if data['state'] == 'starting' else old['started'])
        call['evidence_refs'].append(data['evidence'])
        if data['not_started_evidence'] is not None:
            call['evidence_refs'].append(data['not_started_evidence'])
        lcall(call, data['invocation'], state['limits'], imported=True)
        _lcount_call(state, old, -1); _lcount_call(state, call, 1)
        state['calls'][data['invocation']] = call
        # A direct host observation can settle an imported start uncertainty as well.
        if call['started'] is not None:
            target = lpointer('calls', data['invocation'], 'started')
            state['unresolved'] = [u for u in state['unresolved'] if u['target'] != target]
    elif kind == 'check':
        ledger_require(data['check_id'] not in state['checks'], code='invalid_event_transition')
        state['checks'][data['check_id']] = copy.deepcopy(data)
    else:
        candidate = copy.deepcopy(state)
        resolved = set()
        for patch in data['resolutions']:
            matches = [u for u in candidate['unresolved'] if u['id'] == patch['unresolved_id'] and u['target'] == patch['target']]
            ledger_require(len(matches) == 1 and patch['unresolved_id'] not in resolved, code='invalid_event_transition')
            parts = lparts(patch['target'])
            parent = candidate
            for key in parts[:-1]:
                ledger_require(isinstance(parent, dict) and key in parent, code='invalid_event_transition')
                parent = parent[key]
            ledger_require(parent.get(parts[-1]) is None and patch['expected_value'] is None and patch['value'] is not None, code='invalid_event_transition')
            if parts[0] == 'calls' and parts[-1] == 'started' and patch['value'] is False:
                ledger_require(any(ref.startswith('not-started:') for ref in data['evidence_refs']), code='invalid_event_transition')
            parent[parts[-1]] = copy.deepcopy(patch['value'])
            if parts[0] == 'calls':
                parent['evidence_refs'].extend(data['evidence_refs'])
            resolved.add(patch['unresolved_id'])
        candidate['unresolved'] = [u for u in candidate['unresolved'] if u['id'] not in resolved]
        ledger_validate_opening({k: candidate[k] for k in LEDGER_OPENING_FIELDS}, replay_state=True)
        _lcounters(candidate)
        state.clear(); state.update(candidate)
    _lrefresh(state)
    if owned_counters:
        state.pop('_counts'); state.pop('_membership_unknown')
    return state


def ledger_apply(state, event):
    owned_counters = '_counts' not in state
    try:
        return _ledger_apply(state, event)
    finally:
        if owned_counters:
            state.pop('_counts', None)
            state.pop('_membership_unknown', None)


def ledger_replay(value):
    ledger_type(value, 'object')
    if 'record_type' not in value:
        detected = ledger_format_info(value)
        error = LedgerError('conversion_required' if detected.candidate == 'legacy' else 'invalid_ledger_shape',
                            reason='result_is_not_journal' if detected.candidate == 'result' else None)
        error.detected_format = detected.name
        raise error
    ledger_type(value['record_type'], 'string', '$.record_type')
    ledger_require(value['record_type'] == LEDGER_TYPE, code='unsupported_record_type')
    if 'schema_version' not in value:
        ledger_fields(value, {'schema_version'}, set(value) | {'schema_version'})
    ledger_type(value.get('schema_version'), 'integer', '$.schema_version')
    ledger_require(value['schema_version'] == 1, code='unsupported_schema_version')
    lfields(value, {'record_type', 'schema_version', 'run_id', 'events'})
    ledger_type(value['run_id'], 'string', '$.run_id')
    ledger_type(value['events'], 'array', '$.events')
    ledger_require(lid(value['run_id']) and bool(value['events']))
    state, seen = None, set()
    for index, event in enumerate(value['events']):
        try:
            lfields(event, {'id', 'revision', 'at', 'kind', 'data'})
            ledger_require(lid(event['id']) and event['id'] not in seen and type(event['revision']) is int and
                           event['revision'] == index + 1 and timestamp(event['at']), code='invalid_event_history')
            seen.add(event['id'])
            if index == 0:
                ledger_require(event['kind'] == 'opening', code='invalid_event_history')
                ledger_validate_opening(event['data'])
                state = copy.deepcopy(event['data']) | {'run_id': value['run_id'], 'revision': 1}
                _lcounters(state); _lrefresh(state)
            else:
                ledger_require(event['kind'] != 'opening', code='invalid_event_history')
                ledger_apply(state, event)
                state['revision'] = index + 1
        except LedgerError as error:
            error.event_index = index
            if getattr(error, 'json_path', '$') == '$':
                error.json_path = '$.events[' + str(index) + ']'
            if error.code == 'invalid_request':
                error.code = 'invalid_event_history'
            raise
    state.pop('_counts'); state.pop('_membership_unknown')
    return state


# Legacy interpretation exists only in this explicit converter.
def ledger_convert_keys(value, keys):
    ledger_fields(value, keys, code='invalid_request')


def ledger_convert_refs(value):
    ledger_type(value, 'array', code='invalid_request')
    for reference in value:
        ledger_type(reference, 'string', code='invalid_request')
    ledger_require(type(value) is list and bool(value) and all(nonempty(x) for x in value),
                   'manual_reconciliation_required')


def ledger_convert_mapping(mapping, paths, strategy):
    ledger_convert_keys(mapping, ('actor', 'authority', 'reason', 'quiescence', 'field_mappings',
                                 'unmapped', 'source_relationships', 'opening', 'accounting', 'event_metadata'))
    ledger_require(all(nonempty(mapping[k]) for k in ('actor', 'authority', 'reason')), 'invalid_request')
    ledger_convert_keys(mapping['quiescence'], ('status', 'evidence_refs'))
    ledger_require(mapping['quiescence']['status'] == 'quiescent', 'manual_reconciliation_required')
    ledger_convert_refs(mapping['quiescence']['evidence_refs'])
    ledger_require(type(mapping['field_mappings']) is list and bool(mapping['field_mappings']),
                   'manual_reconciliation_required')
    for field in mapping['field_mappings']:
        ledger_convert_keys(field, ('source', 'source_pointer', 'target', 'evidence_refs'))
        ledger_require(field['source'] in paths and type(field['source_pointer']) is str
                       and field['source_pointer'].startswith('/') and nonempty(field['target']), 'invalid_request')
        ledger_convert_refs(field['evidence_refs'])
    ledger_require(type(mapping['unmapped']) is list and type(mapping['source_relationships']) is list,
                   'invalid_request')
    for field in mapping['unmapped']:
        ledger_convert_keys(field, ('source', 'source_pointer', 'reason'))
        ledger_require(field['source'] in paths and nonempty(field['source_pointer']) and nonempty(field['reason']),
                       'invalid_request')
    related = set(); related_pairs = set()
    for relation in mapping['source_relationships']:
        ledger_convert_keys(relation, ('sources', 'relation', 'evidence_refs'))
        for source in relation['sources']:
            ledger_type(source, 'string', code='invalid_request')
        ledger_require(type(relation['sources']) is list and len(relation['sources']) >= 1
                       and len(set(relation['sources'])) == len(relation['sources'])
                       and all(p in paths for p in relation['sources'])
                       and relation['relation'] in ('independent', 'overlap', 'aggregate', 'unknown'), 'invalid_request')
        ledger_convert_refs(relation['evidence_refs']); related.update(relation['sources'])
        for left in relation['sources']:
            for right in relation['sources']:
                if left != right: related_pairs.add(frozenset((left, right)))
    ledger_require(len(paths) == 1 or related == set(paths), 'manual_reconciliation_required')
    ledger_require(all(frozenset((left, right)) in related_pairs for left in paths for right in paths
                       if left != right), 'manual_reconciliation_required')
    targets = [f['target'] for f in mapping['field_mappings']]
    ledger_require(len(set(targets)) == len(targets), 'manual_reconciliation_required',
                   reason='conflicting_field_mapping')
    ledger_require(type(mapping['accounting']) is dict and type(mapping['event_metadata']) is dict,
                   'invalid_request')
    ledger_require((strategy == 'opening' and type(mapping['opening']) is dict)
                   or (strategy == 'journal' and mapping['opening'] is None), 'invalid_request')


def ledger_convert_accounting(opening, mapping):
    ledger_require(set(mapping['accounting']) == set(opening['opening_used']), 'manual_reconciliation_required')
    relation_unknown = any(r['relation'] == 'unknown' for r in mapping['source_relationships'])
    for bucket, baseline in opening['opening_used'].items():
        record = mapping['accounting'][bucket]
        ledger_convert_keys(record, ('reported_used', 'baseline_used', 'materialized_started', 'evidence_refs'))
        ledger_convert_refs(record['evidence_refs'])
        for key in ('reported_used', 'baseline_used', 'materialized_started'):
            ledger_require(record[key] is None or type(record[key]) is int and record[key] >= 0, 'invalid_request')
        ledger_require(record['baseline_used'] == baseline, 'manual_reconciliation_required')
        calls = list(opening['calls'].values())
        unknown = relation_unknown or any(c['buckets'] is None or
                   bucket in c['buckets'] and c['started'] is None for c in calls)
        materialized = None if unknown else sum(c['started'] is True and bucket in c['buckets'] for c in calls)
        ledger_require(record['materialized_started'] == materialized, 'manual_reconciliation_required')
        if unknown:
            ledger_require(baseline is None and record['reported_used'] is None, 'manual_reconciliation_required')
        elif baseline is None:
            ledger_require(record['reported_used'] is None, 'manual_reconciliation_required')
        else:
            ledger_require(record['reported_used'] == baseline + materialized, 'manual_reconciliation_required')


def ledger_convert_legacy(source, source_path, mapping):
    ledger_convert_keys(source, ('schema_version', 'events'))
    ledger_require(type(source['schema_version']) is int and source['schema_version'] == 1
                   and type(source['events']) is list, 'invalid_ledger_shape')
    limits, calls, checks, events, mappings, seen = {}, {}, {}, [], [], set()
    # The imported empty opening has zero baseline; limit events establish scopes explicitly.
    opening = {'limits': {}, 'opening_used': {}, 'calls': {}, 'checks': {}, 'provenance': None, 'unresolved': []}
    used_ids = {'opening'}
    for index, event in enumerate(source['events']):
        ledger_convert_keys(event, ('id', 'revision', 'at', 'kind', 'data'))
        ledger_require(type(event['revision']) is int and event['revision'] == index + 1
                       and nonempty(event['id']) and event['id'] not in seen and timestamp(event['at']),
                       'invalid_event_history', reason='invalid_legacy_history')
        seen.add(event['id']); data = event['data']; kind = event['kind']
        ledger_require(type(data) is dict, 'invalid_event_history')
        if kind == 'limit':
            ledger_convert_keys(data, ('bucket', 'value', 'origin', 'authority', 'reason'))
            ledger_require(type(data['value']) is int and data['value'] >= 0
                           and data['origin'] in ('operating', 'user', 'host')
                           and nonempty(data['authority']) and nonempty(data['reason']), 'invalid_event_history')
            previous = limits.get(data['bucket'])
            ledger_require(previous is None or previous['origin'] == data['origin'] == 'operating',
                           'invalid_event_history', reason='legacy_hard_ceiling_changed')
            if previous:
                committed = sum(data['bucket'] in c['buckets'] and
                    (c['started'] or c['state'] in ('reserved', 'starting', 'unknown')) for c in calls.values())
                ledger_require(data['value'] >= committed, 'invalid_event_history')
            metadata = mapping['event_metadata'].get(event['id'])
            ledger_require(metadata is not None, 'manual_reconciliation_required')
            ledger_convert_keys(metadata, ('decision_owner', 'remaining_work_estimate', 'scope_basis', 'evidence_refs'))
            ledger_convert_refs(metadata['evidence_refs'])
            old_entry = {k: previous[k] for k in ('value', 'authority', 'reason')} if previous else None
            new_data = {**data, 'decision_owner': metadata['decision_owner'],
                        'remaining_work_estimate': metadata['remaining_work_estimate'],
                        'scope_basis': metadata['scope_basis'], 'expected_previous': old_entry}
            limits[data['bucket']] = data.copy()
        elif kind == 'reserve':
            ledger_convert_keys(data, ('invocation', 'role', 'unit', 'incident', 'purpose', 'model', 'effort',
                                      'buckets', 'writer', 'workspace'))
            ledger_require(type(data['writer']) is bool and all(nonempty(data[k]) for k in
                           ('invocation', 'role', 'purpose', 'model', 'effort', 'workspace'))
                           and type(data['unit']) is str and type(data['incident']) is str
                           and type(data['buckets']) is list and data['invocation'] not in calls, 'invalid_event_history')
            calls[data['invocation']] = {**data, 'state': 'reserved', 'started': False}
            new_data = dict(data)
        elif kind == 'transition':
            ledger_convert_keys(data, ('invocation', 'state', 'evidence', 'outcome', 'observed_model'))
            call = calls.get(data['invocation'])
            ledger_require(call is not None and data['state'] in TRANSITIONS.get(call['state'], set())
                           and nonempty(data['evidence']) and nonempty(data['observed_model']), 'invalid_event_transition')
            # Old unknown->cancelled cannot prove absence merely from generic prose.
            ledger_require(not (call['state'] == 'unknown' and data['state'] == 'cancelled'),
                           'manual_reconciliation_required', reason='legacy_nonstart_evidence_missing')
            call['state'] = data['state']; call['started'] |= data['state'] in ('running', 'finished')
            new_data = {**data, 'observed_effort': 'unverified', 'not_started_evidence': None}
        elif kind == 'check':
            ledger_convert_keys(data, ('check_id', 'path', 'sha256'))
            ledger_require(data['check_id'] not in checks, 'invalid_event_history')
            checks[data['check_id']] = data; new_data = dict(data)
        else:
            raise LedgerError('invalid_event_history', event_index=index)
        old_id = event['id']
        safe = bool(__import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,95}', old_id))
        new_id = old_id if safe and old_id not in used_ids else 'import-' + hashlib.sha256(
            (str(index) + ':' + old_id).encode()).hexdigest()[:32]
        while new_id in used_ids:
            new_id = 'import-' + hashlib.sha256(new_id.encode()).hexdigest()[:32]
        used_ids.add(new_id)
        events.append({'id': new_id, 'revision': index + 2, 'at': event['at'], 'kind': kind, 'data': new_data})
        mappings.append({'source': source_path, 'old_id': old_id, 'old_revision': index + 1,
                         'new_id': new_id, 'new_revision': index + 2})
    ledger_require(set(mapping['event_metadata']) == {e['id'] for e in source['events'] if e['kind'] == 'limit'},
                   'invalid_request')
    return opening, events, mappings, calls, limits


def ledger_convert(sources, destination, run_id, mode, strategy, mapping, preservation_directory):
    ledger_require(mode in ('check', 'write') and strategy in ('journal', 'opening'), 'invalid_request')
    ledger_require(type(sources) is list and bool(sources), 'invalid_request')
    destination = ledger_destination(destination)
    preserve = ledger_path(preservation_directory)
    ledger_require(preserve.parent.is_dir() and not preserve.is_symlink(), 'invalid_request')
    paths, source_records, inode_seen = [], [], set()
    for source in sources:
        ledger_convert_keys(source, ('path', 'expected_sha256'))
        ledger_require(sha256(source['expected_sha256']), 'invalid_request')
        path = ledger_path(source['path']); raw, inode = ledger_source(path)
        ledger_require(hashlib.sha256(raw).hexdigest() == source['expected_sha256'], 'source_changed')
        ledger_require(inode not in inode_seen, 'invalid_request'); inode_seen.add(inode)
        paths.append(str(path)); source_records.append((path, raw, inode, ledger_decode(raw)))
    lock_path = ledger_lock_path(destination)
    pairs = [(destination, preserve), (preserve, lock_path)]
    for path, _, _, _ in source_records:
        pairs.extend((path, protected) for protected in (destination, preserve, lock_path))
    ledger_require(len(paths) == len(set(paths)) and
                   not any(paths_overlap(left, right) for left, right in pairs), 'invalid_request')
    ledger_convert_mapping(mapping, paths, strategy)
    # Validate claimed JSON locations without assuming their values prove a semantic mapping.
    source_values = {str(p): v for p, _, _, v in source_records}
    for field in mapping['field_mappings']:
        current = source_values[field['source']]
        try:
            for token in field['source_pointer'].split('/')[1:]:
                token = token.replace('~1', '/').replace('~0', '~')
                current = current[int(token)] if type(current) is list else current[token]
        except (ValueError, TypeError, KeyError, IndexError):
            raise LedgerError('manual_reconciliation_required', reason='source_pointer_missing') from None
    identity = digest({'sources': [{'path': str(p.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}
                                  for p, raw, _, _ in source_records],
                       'destination': str(destination.resolve()), 'strategy': strategy, 'run_id': run_id, 'mapping': mapping})
    metadata_path = preserve / 'conversion.json'
    if preserve.exists():
        ledger_require(preserve.is_dir(), 'destination_exists')
        raw_meta, _ = ledger_source(metadata_path)
        ledger_require(ledger_decode(raw_meta) == {'request_identity': identity}, 'destination_exists')
        allowed = {'conversion.json'} | {f'{i:04d}.json' for i in range(len(sources))}
        ledger_require(all(p.name in allowed for p in preserve.iterdir()), 'destination_exists')
    def candidate():
        if strategy == 'journal':
            ledger_require(len(source_records) == 1, 'manual_reconciliation_required')
            opening, events, event_mappings, legacy_calls, legacy_limits = ledger_convert_legacy(source_records[0][3], paths[0], mapping)
        else:
            opening = json.loads(json.dumps(mapping['opening'])); events, event_mappings = [], []
            ledger_require(opening.get('provenance') is None, 'invalid_request')
        opening['provenance'] = {'sources': [
            {'path': str(p), 'sha256': hashlib.sha256(raw).hexdigest(),
             'detected_format': ledger_classify(value), 'preserved_path': str(preserve / f'{i:04d}.json')}
            for i, (p, raw, _, value) in enumerate(source_records)],
            'converter_version': '1', 'schema_version': 1, 'converted_at': now(),
            **{k: mapping[k] for k in ('actor', 'authority', 'reason', 'field_mappings', 'unmapped',
                                     'source_relationships', 'quiescence')},
            'event_mappings': event_mappings, 'history_level': 'full-events' if strategy == 'journal' else 'opening-summary',
            'request_identity': identity, 'accounting': copy.deepcopy(mapping['accounting']),
            'event_metadata': copy.deepcopy(mapping['event_metadata'])}
        value = {'record_type': 'workflow-invocation-journal', 'schema_version': 1, 'run_id': run_id,
                 'events': [{'id': 'opening', 'revision': 1, 'at': now(), 'kind': 'opening', 'data': opening}] + events}
        state = ledger_replay(value)
        if strategy == 'opening':
            ledger_convert_accounting(opening, mapping)
            required = ['/opening_used/' + b.replace('~', '~0').replace('/', '~1') for b in opening['opening_used']]
            required += ['/calls/' + i for i in opening['calls']]
            required += ['/limits/' + b.replace('~', '~0').replace('/', '~1') + '/' + origin
                         for b, origins in opening['limits'].items() for origin in origins]
            # Each accounting or assignment fact needs its own source location. Broad root
            # mappings would conceal duplicate memberships and contradictory assignments.
            supplied = {f['target'] for f in mapping['field_mappings']}
            ledger_require(all(target in supplied for target in required), 'manual_reconciliation_required',
                           reason='incomplete_field_mapping')
        else:
            ledger_require(not mapping['accounting'], 'invalid_request')
            for bucket in legacy_limits:
                old_used = sum(c['started'] and bucket in c['buckets'] for c in legacy_calls.values())
                new_used = sum(c['started'] is True and bucket in (c['buckets'] or []) for c in state['calls'].values())
                old_reserved = sum(not c['started'] and c['state'] in ('reserved', 'starting', 'unknown')
                                   and bucket in c['buckets'] for c in legacy_calls.values())
                new_reserved = sum(c['started'] is not True and c['state'] in ('reserved', 'starting', 'unknown')
                                   and bucket in (c['buckets'] or []) for c in state['calls'].values())
                ledger_require(old_used == new_used and old_reserved == new_reserved,
                               'manual_reconciliation_required', reason='legacy_accounting_mismatch')
        return value, state
    value, state = candidate()
    def verify_sources():
        for path, raw, inode, _ in source_records:
            try: current, current_inode = ledger_source(path)
            except LedgerError:
                raise LedgerError('source_changed') from None
            ledger_require(inode == current_inode and raw == current, 'source_changed')
    # Validate any existing partial copies even in preview; do not silently repair them.
    for i, (_, raw, _, _) in enumerate(source_records):
        copy_path = preserve / f'{i:04d}.json'
        if copy_path.exists() or copy_path.is_symlink():
            copied, copied_inode = ledger_source(copy_path)
            ledger_require(copied == raw and copied_inode not in inode_seen, 'destination_exists')
    mutation = {'ledger': 'none', 'artifacts': 'none'}
    if mode == 'write':
        publishing_destination = False
        try:
            with ledger_lock(destination):
                mutation['artifacts'] = 'created'
                ledger_destination(destination); verify_sources(); value, state = candidate()
                if not preserve.exists():
                    preserve.mkdir(mode=0o700)
                    directory_fd = os.open(preserve.parent, os.O_RDONLY)
                    try: os.fsync(directory_fd)
                    finally: os.close(directory_fd)
                    ledger_publish(metadata_path, encoded({'request_identity': identity}))
                else:
                    raw_meta, _ = ledger_source(metadata_path)
                    ledger_require(ledger_decode(raw_meta) == {'request_identity': identity}, 'destination_exists')
                for i, (_, raw, _, _) in enumerate(source_records):
                    copy_path = preserve / f'{i:04d}.json'
                    if not copy_path.exists(): ledger_publish(copy_path, raw)
                    copied, copied_inode = ledger_source(copy_path)
                    ledger_require(copied == raw and copied_inode not in inode_seen, 'source_changed')
                verify_sources()
                publishing_destination = True
                ledger_publish(destination, encoded(value))
                mutation['ledger'] = 'committed'
        except LedgerError as error:
            prior = getattr(error, 'mutation', None)
            if publishing_destination and prior and prior.get('ledger') == 'unknown': mutation['ledger'] = 'unknown'
            error.mutation = mutation
            raise
        except OSError:
            error = LedgerError('write_failed', phase='preservation')
            error.mutation = mutation
            raise error from None
    result = ledger_summary(state)
    result.update({'mutation': mutation, 'conversion': {'mode': mode, 'strategy': strategy,
                   'destination': str(destination), 'request_identity': identity,
                   'preservation_directory': str(preserve), 'history_level': state['provenance']['history_level']}})
    return result


LEVELS = ['static', 'fixture', 'skill_behavior', 'runtime']
CHECK_FIELDS = {'schema_version', 'check_id', 'requirements_identity', 'argv', 'cwd',
                'environment', 'started_at', 'finished_at', 'exit_code', 'timed_out',
                'log', 'log_sha256', 'before', 'after', 'stable', 'items'}


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def strings(value, unique=False, non_empty=False):
    return (isinstance(value, list) and all(isinstance(v, str) and (not non_empty or nonempty(v)) for v in value)
            and (not unique or len(value) == len(set(value))))


def timestamp(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def absolute(value):
    return nonempty(value) and '\x00' not in value and Path(value).is_absolute()


def relative(value):
    return nonempty(value) and '\x00' not in value and not Path(value).is_absolute() and '..' not in Path(value).parts


def argv_valid(value):
    return strings(value) and bool(value) and nonempty(value[0]) and all('\x00' not in v for v in value)


def environment_valid(value):
    return nonempty(value) or isinstance(value, dict) and bool(value)


def fields(value, rules, path):
    """Collect exact object-field errors without coercing values or aborting siblings."""
    if not isinstance(value, dict):
        return [('unknowns', path + ': expected object')]
    errors = [('missing', path + '.' + k) for k in rules if k not in value]
    errors += [('unknowns', path + '.' + k + ': unsupported field') for k in sorted(set(value) - rules.keys())]
    errors += [('unknowns', path + '.' + k + ': invalid value') for k, test in rules.items() if k in value and not test(value[k])]
    return errors


def check_errors(value, path='check'):
    errors = fields(value, {
        'schema_version': lambda v: type(v) is int and v == 1,
        'check_id': nonempty, 'requirements_identity': sha256, 'argv': argv_valid,
        'cwd': absolute, 'environment': environment_valid, 'started_at': timestamp,
        'finished_at': timestamp, 'exit_code': lambda v: type(v) is int,
        'timed_out': lambda v: type(v) is bool, 'log': absolute, 'log_sha256': sha256,
        'before': sha256, 'after': sha256, 'stable': lambda v: type(v) is bool,
        'items': lambda v: isinstance(v, list)}, path)
    if isinstance(value, dict) and isinstance(value.get('items'), list):
        for index, item in enumerate(value['items']):
            errors += fields(item, {
                'id': nonempty, 'acceptance_ids': lambda v: strings(v, True, True) and bool(v),
                'result': lambda v: isinstance(v, str) and v in ('passed', 'failed', 'not_run'),
                'evidence': nonempty, 'unknowns': strings,
                'observed_evidence_level': lambda v: v in LEVELS}, f'{path}.items[{index}]')
    return errors


def regular_stream(path):
    """Open without following symlinks or blocking on a replaced special file."""
    require(stat.S_ISREG(Path(path).lstat().st_mode), 'Expected regular file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, 'rb')
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise ValueError('Expected regular file')
    return stream


def regular_bytes(path):
    with regular_stream(path) as stream:
        return stream.read()


def log_error(check):
    """Report identity only, never log contents."""
    try:
        with regular_stream(check['log']) as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != check['log_sha256']:
                return 'log_mismatch'
    except (OSError, ValueError):
        return 'missing_log'
    return None


def requirements(value):
    errors = fields(value, {
        'schema_version': lambda v: type(v) is int and v == 1,
        'revision': nonempty, 'authority': nonempty,
        'acceptance_ids': lambda v: strings(v, True, True) and bool(v),
        'gate_items': lambda v: isinstance(v, list) and bool(v)}, 'requirements')
    require(not errors, 'Invalid requirements: ' + str(errors))
    gates = value['gate_items']
    for gate in gates:
        errors += fields(gate, {'id': nonempty,
            'acceptance_ids': lambda v: strings(v, True, True) and bool(v),
            'required_evidence_level': lambda v: v in LEVELS}, 'requirements.gate_items')
    require(not errors, 'Invalid requirements: ' + str(errors))
    require(len({g['id'] for g in gates}) == len(gates), 'Duplicate gates')
    ac = set(value['acceptance_ids'])
    require(all(set(g['acceptance_ids']) <= ac for g in gates), 'Invalid gate requirement')
    require(ac == {a for g in gates for a in g['acceptance_ids']}, 'Acceptance missing required gate')
    return digest(value)


def validate_plan(plan, approved):
    errors = []
    identity = requirements(approved)
    if plan.get('requirements_identity') != identity:
        errors.append('requirements identity mismatch')
    tickets = plan.get('tickets', [])
    by_id = {t['id']: t for t in tickets}
    if not tickets or len(by_id) != len(tickets):
        errors.append('missing/duplicate ticket IDs')
    dependencies, children = {}, {name: [] for name in by_id}
    for name, ticket in by_id.items():
        if 'routing_hint' in ticket:
            hint = ticket['routing_hint']
            if (not isinstance(hint, dict)
                    or set(hint) != {'reason_codes', 'evidence_refs'}
                    or not all(strings(hint[key], non_empty=True) for key in ('reason_codes', 'evidence_refs'))):
                errors.append('invalid routing hint: ' + name)
        dependencies[name] = set(ticket.get('depends_on', []))
        for parent in dependencies[name]:
            if parent not in by_id:
                errors.append('missing dependency: ' + parent)
            else:
                children[parent].append(name)
    degree = {name: len(parents) for name, parents in dependencies.items()}
    ready = deque(name for name in by_id if degree[name] == 0)
    closure = {}
    while ready:
        name = ready.popleft()
        closure[name] = {name}
        for parent in dependencies[name]:
            closure[name].update(closure[parent])
        for child in children[name]:
            degree[child] -= 1
            if degree[child] == 0:
                ready.append(child)
    missing_blocked = {name for name, parents in dependencies.items() if not parents <= by_id.keys()}
    pending = deque(missing_blocked)
    while pending:
        for child in children[pending.popleft()]:
            if child not in missing_blocked:
                missing_blocked.add(child); pending.append(child)
    for name in by_id:
        if name not in closure:
            errors.append(('unresolved dependencies: ' if name in missing_blocked
                           else 'dependency cycle or blocked predecessor: ') + name)
    ac = set(approved['acceptance_ids'])
    coverage = {a for t in tickets for a in t.get('acceptance_ids', [])}
    if coverage != ac:
        errors.append('ticket acceptance coverage mismatch')
    gates = plan.get('gate_items', [])
    if len({g['id'] for g in gates}) != len(gates):
        errors.append('duplicate gate IDs')
    if {g['id'] for g in gates} != {g['id'] for g in approved['gate_items']}:
        errors.append('required gate inventory mismatch')
    for gate in gates:
        owner = gate.get('owner_ticket')
        suppliers = set(gate.get('requires_tickets', []))
        if owner not in closure or not suppliers or not suppliers <= closure.get(owner, set()):
            errors.append('invalid gate suppliers: ' + gate['id'])
        matching = next((g for g in approved['gate_items'] if g['id'] == gate['id']), None)
        if matching and any(gate.get(k) != matching[k] for k in ('acceptance_ids', 'required_evidence_level')):
            errors.append('gate differs from approved requirements: ' + gate['id'])
        supplied_ac = {a for name in suppliers for a in by_id.get(name, {}).get('acceptance_ids', [])}
        if not set(gate.get('acceptance_ids', [])) <= supplied_ac:
            errors.append('gate acceptance not supplied: ' + gate['id'])
    return {'valid': not errors, 'errors': errors, 'requirements_identity': identity}


def run_check(root, approved, argv, environment, log, check_id, watch=(), exclude=(), timeout=300):
    require(argv_valid(argv), 'Invalid argv: expected a non-empty list of strings (no shell expansion)')
    require(environment_valid(environment), 'Invalid environment: expected a sanitized description string or non-empty object')
    require(nonempty(check_id), 'Invalid check_id: expected a non-empty string')
    require(isinstance(root, (str, Path)) and nonempty(str(root)) and '\x00' not in str(root), 'Invalid root: expected the Git workspace root path')
    require(isinstance(log, (str, Path)) and nonempty(str(log)) and '\x00' not in str(log), 'Invalid log: expected a new file path')
    require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0, 'Invalid timeout: expected positive seconds')
    require(isinstance(watch, (list, tuple)) and all(relative(v) for v in watch), 'Invalid watch: expected project-relative paths')
    require(isinstance(exclude, (list, tuple)) and all(relative(v) for v in exclude), 'Invalid exclude: expected project-relative paths or globs')
    req = requirements(approved)
    before = snapshot(root, watch, exclude)
    started = now()
    # Only a user-selected log file receives output; never copy the entire environment.
    with Path(log).open('xb') as stream:
        process = subprocess.Popen(argv, cwd=root, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    after = snapshot(root, watch, exclude)
    return {'schema_version': 1, 'check_id': check_id, 'requirements_identity': req, 'argv': argv,
            'cwd': str(Path(root).resolve()), 'environment': environment, 'started_at': started, 'finished_at': now(),
            'exit_code': process.returncode, 'timed_out': timed_out, 'log': str(Path(log).resolve()),
            'log_sha256': hashlib.sha256(Path(log).read_bytes()).hexdigest(),
            'before': before['candidate'], 'after': after['candidate'],
            'stable': before['stable'] and after['stable'] and before['candidate'] == after['candidate'],
            'items': []}


def validate_gate(approved, candidate, checks):
    req = requirements(approved)
    require(sha256(candidate), 'Invalid candidate: expected a 64-hex candidate id (a check "after" value or snapshot candidate)')
    require(isinstance(checks, list), 'Invalid checks: expected a list of check record paths')
    issues, valid = [], []
    for index, check in enumerate(checks):
        errors = check_errors(check)
        if errors:
            issue = {'check_index': index, 'kinds': ['unrecognized_record'],
                     'details': [message for _, message in errors]}
            if isinstance(check, dict) and nonempty(check.get('check_id')):
                issue['check_id'] = check['check_id']
            issues.append(issue)
        else:
            valid.append(check)
    for gate in approved['gate_items']:
        found = [(c, i) for c in valid for i in c['items'] if i['id'] == gate['id']]
        if not found:
            issues.append({'id': gate['id'], 'kind': 'missing'}); continue
        # Conflicting evidence is reported, never silently prefer the latest pass.
        for check, item in found:
            reasons = []
            if check['requirements_identity'] != req or check['before'] != candidate or check['after'] != candidate or not check['stable']:
                reasons.append('stale')
            if check['exit_code'] != 0 or check['timed_out'] or item['result'] != 'passed':
                reasons.append('failed_or_not_run')
            if item['unknowns']:
                reasons.append('unknown')
            if set(item['acceptance_ids']) != set(gate['acceptance_ids']):
                reasons.append('acceptance_mismatch')
            if LEVELS.index(item['observed_evidence_level']) < LEVELS.index(gate['required_evidence_level']):
                reasons.append('evidence_level')
            error = log_error(check)
            if error:
                reasons.append(error)
            if reasons:
                issues.append({'id': gate['id'], 'check_id': check['check_id'], 'kinds': reasons})
    return {'passed': not issues, 'issues': issues}


SECTIONS = {'summary': '시작 요약', 'authority': '사용자 결정과 권한', 'status': '구현·검증 상태',
            'active_work': '진행 중인 작업', 'attempts': '시도와 배운 점', 'next_steps': '다음 행동'}


def reference(root, entry):
    require(set(entry) == {'path', 'why'}, 'Reference requires path and reading purpose')
    path = safe_path(root, entry['path'])
    require(not path.is_symlink(), 'Use canonical reference, not symlink')
    return {**entry, 'sha256': hashlib.sha256(regular_bytes(path)).hexdigest() if path.is_file() else None}


def handoff_create(root, draft, output, watch=(), exclude=()):
    root = Path(root).resolve()
    require(set(draft) <= set(SECTIONS) | {'target', 'purpose', 'references', 'previous', 'writers', 'ledger', 'checks'}, 'Unknown handoff draft field')
    require(draft.get('target') and draft.get('purpose'), 'Target and purpose required')
    output = safe_path(root, output)
    require(not output.exists() and not output.is_symlink(), 'Preserve existing handoff; choose new ID')
    exclusions = list(exclude)
    rel_output = output.relative_to(root).as_posix()
    require(rel_output not in {'.', ''}, 'Use a new handoff directory')
    if not matches(rel_output, exclusions):
        exclusions.append(rel_output)
    before = snapshot(root, watch, exclusions)
    refs = [reference(root, r) for r in draft.get('references', [])]
    records, unknowns = {}, []
    for key in ('ledger', 'checks'):
        for name in ([draft[key]] if key == 'ledger' and draft.get(key) else draft.get(key, []) if key == 'checks' else []):
            ref = reference(root, {'path': name, 'why': key + ' reconciliation'})
            refs.append(ref)
            try:
                record = ledger_snapshot(safe_path(root, name), name) if key == 'ledger' else read(safe_path(root, name))
                if key == 'ledger':
                    ref['sha256'] = record['ledger']['sha256']
                if key == 'checks' and check_errors(record):
                    unknowns.append('unrecognized check format: ' + name)
                    records[name] = {'unrecognized_record': record}
                else:
                    records[name] = record
            except (ValueError, KeyError, TypeError, OSError):
                unknowns.append('unrecognized or missing record: ' + name)
    writers = draft.get('writers', {'status': 'unknown', 'observed_at': now(), 'evidence': 'not supplied'})
    if writers.get('status') != 'quiescent' or not writers.get('evidence') or not writers.get('observed_at'):
        unknowns.append('writer state requires reconciliation')
    for value in records.values():
        for invocation, call in value.get('active_calls', {}).items():
            if call.get('state') in {'starting', 'unknown', 'running'}:
                unknowns.append('active recorded assignment: ' + invocation)
        if value.get('unresolved'):
            unknowns.append('ledger facts require reconciliation')
    body = '# 작업 인계: ' + draft['target'] + '\n\n인계 목적: ' + draft['purpose'] + '\n'
    for key, title in SECTIONS.items():
        body += '\n## ' + title + '\n\n' + draft.get(key, '미확인: 작성 필요') + '\n'
    body += '\n## 읽기 경로\n\n'
    for ref in refs:
        body += '- `' + ref['path'] + '` — ' + ref['why'] + '\n'
    if draft.get('previous'):
        previous = reference(root, {'path': draft['previous'], 'why': '이전 인계 이력; 현재 정본을 우선'})
        refs.append(previous)
        body += '- `' + previous['path'] + '` — ' + previous['why'] + '\n'
    after = snapshot(root, watch, exclusions)
    unknowns += before['unknowns'] + after['unknowns']
    changed_refs = [r['path'] for r in refs if reference(root, {'path': r['path'], 'why': r['why']})['sha256'] != r['sha256']]
    stable = before['stable'] and after['stable'] and before['candidate'] == after['candidate'] and not unknowns and not changed_refs
    manifest = {'schema_version': 2, 'created_at': now(), 'target': draft['target'], 'purpose': draft['purpose'],
                'candidate': before, 'stable': stable, 'references': refs, 'records': records,
                'writers': writers, 'unknowns': unknowns, 'changed_references': changed_refs,
                'sections': {k: bool(draft.get(k)) for k in SECTIONS},
                'document_sha256': hashlib.sha256(body.encode()).hexdigest()}
    output.mkdir(parents=True, exist_ok=False)
    (output / 'HANDOFF.md').write_text(body)
    exclusive_json(output / 'manifest.json', manifest)
    return {'path': str(output / 'HANDOFF.md'), 'stable': stable,
            'starter': f"codex-swarm handoff의 수신 절차로 {rel_output}/HANDOFF.md를 읽고 목표·권한·실제 상태·검증 근거를 먼저 대조하세요. 현재 요청 범위에서 다음 행동을 판단하세요.",
            'validation': handoff_validate(root, output)}


def candidate_errors(value, path):
    errors = fields(value, {
        'schema_version': lambda v: type(v) is int and v == 1, 'candidate': sha256,
        'identity': lambda v: isinstance(v, dict), 'unknowns': strings,
        'observed_at': timestamp, 'workspace': absolute, 'stable': lambda v: type(v) is bool}, path)
    if not isinstance(value, dict) or not isinstance(value.get('identity'), dict):
        return errors
    identity = value['identity']; base = path + '.identity'
    oid = lambda v: isinstance(v, str) and len(v) in (40, 64) and all(c in '0123456789abcdef' for c in v)
    errors += fields(identity, {'files': lambda v: isinstance(v, dict), 'index': lambda v: isinstance(v, list),
        'head': lambda v: v == '' or oid(v),
        'watch': lambda v: strings(v) and all(relative(x) for x in v),
        'exclude': lambda v: strings(v) and all(relative(x) for x in v)}, base)
    mode = lambda v: type(v) is int and 0 <= v <= 0o7777
    variants = {
        'deleted': {}, 'file': {'mode': mode, 'sha256': sha256},
        'symlink': {'mode': mode, 'target': lambda v: isinstance(v, str)},
        'submodule': {'head': lambda v: v == '' or oid(v), 'status': lambda v: isinstance(v, str)}}
    if isinstance(identity.get('files'), dict):
        for name, entry in identity['files'].items():
            entry_path = base + '.files.' + name
            if not relative(name):
                errors.append(('unknowns', entry_path + ': invalid path'))
            kind = entry.get('kind') if isinstance(entry, dict) else None
            if not isinstance(kind, str) or kind not in variants:
                errors.append(('unknowns', entry_path + ': invalid file kind')); continue
            errors += fields(entry, {'kind': lambda v: v == kind, **variants[kind]}, entry_path)
    if isinstance(identity.get('index'), list):
        for i, entry in enumerate(identity['index']):
            rules = {'path': relative, 'mode': lambda v: v in ('100644', '100755', '120000', '160000'),
                     'oid': oid, 'stage': lambda v: v in ('0', '1', '2', '3')}
            if not isinstance(entry, dict) or entry.get('mode') != '160000':
                rules['sha256'] = sha256
            errors += fields(entry, rules, f'{base}.index[{i}]')
    return errors


def ledger_state_errors(value, path='ledger'):
    """Validate compact snapshots without reintroducing an old operational parser."""
    errors = fields(value, {
        'record_type': lambda v: v == 'workflow-invocation-snapshot',
        'schema_version': lambda v: type(v) is int and v == 1,
        'ledger': lambda v: isinstance(v, dict), 'limits_by_origin': lambda v: isinstance(v, dict),
        'usage': lambda v: isinstance(v, dict), 'active_calls': lambda v: isinstance(v, dict),
        'unresolved': lambda v: isinstance(v, list)}, path)
    if errors:
        return errors
    errors += fields(value['ledger'], {'path': relative, 'sha256': sha256,
        'run_id': lid, 'revision': lambda v: type(v) is int and v >= 1}, path + '.ledger')
    count = lambda v: type(v) is int and v >= 0
    nullable_count = lambda v: v is None or count(v)
    for index, (bucket, entries) in enumerate(value['limits_by_origin'].items()):
        base = path + '.limits_by_origin[' + str(index) + ']'
        if not lbucket(bucket) or not isinstance(entries, dict) or not set(entries) <= {'operating', 'user', 'host'}:
            errors.append(('unknowns', base + ': invalid bucket origins')); continue
        for origin, entry in entries.items():
            errors += fields(entry, {'value': count, 'authority': nonempty, 'reason': nonempty}, base + '.' + origin)
    if set(value['usage']) != set(value['limits_by_origin']):
        errors.append(('unknowns', path + ': bucket inventory mismatch'))
    for index, (bucket, usage) in enumerate(value['usage'].items()):
        base = path + '.usage[' + str(index) + ']'
        sub = fields(usage, {'limits_by_origin': lambda v: isinstance(v, dict),
            'effective_limit': nullable_count, 'binding_origins': lambda v: strings(v, True),
            'used': nullable_count, 'reserved': count,
            'available': lambda v: v is None or type(v) is int, 'usage_known': lambda v: type(v) is bool}, base)
        errors += sub
        if not sub:
            origins = value['limits_by_origin'].get(bucket)
            if usage['limits_by_origin'] != origins:
                errors.append(('unknowns', base + ': origin mismatch'))
            if isinstance(origins, dict) and all(isinstance(e, dict) and count(e.get('value')) for e in origins.values()):
                effective = min((e['value'] for e in origins.values()), default=None)
                binding = [o for o in ('operating', 'user', 'host') if o in origins and origins[o]['value'] == effective]
                available = None if effective is None or usage['used'] is None else effective - usage['used'] - usage['reserved']
                if (usage['effective_limit'], usage['binding_origins'], usage['available'], usage['usage_known']) != (effective, binding, available, usage['used'] is not None):
                    errors.append(('unknowns', base + ': arithmetic mismatch'))
    for index, (name, call) in enumerate(value['active_calls'].items()):
        base = path + '.active_calls[' + str(index) + ']'
        errors += fields(call, {'state': lambda v: isinstance(v, str) and v in ('reserved', 'starting', 'running', 'unknown'),
            'started': lambda v: v is None or type(v) is bool,
            'buckets': lambda v: v is None or strings(v, True, True)}, base)
        if not lid(name): errors.append(('unknowns', base + ': invalid ID'))
        if isinstance(call, dict) and {'state', 'started'} <= call.keys() and isinstance(call['state'], str):
            allowed = {'reserved': [False], 'starting': [None], 'running': [True], 'unknown': [True, False, None]}
            if not any(call['started'] is x for x in allowed.get(call['state'], [])):
                errors.append(('unknowns', base + ': state/start mismatch'))
    for index, entry in enumerate(value['unresolved']):
        errors += fields(entry, {'id': lid, 'reason': nonempty,
            'buckets': lambda v: v is None or strings(v, True, True),
            'next_action': lambda v: v == 'resolve_import'}, path + '.unresolved[' + str(index) + ']')
    return errors


def handoff_validate(root, directory):
    for name, value in (('root', root), ('directory', directory)):
        require(isinstance(value, (str, Path)) and nonempty(str(value)) and '\x00' not in str(value), 'Invalid ' + name)
    root, directory = Path(root).resolve(), Path(directory)
    result = {'missing': [], 'reference_errors': [], 'stale': [], 'mismatches': [], 'unknowns': []}
    def report(errors):
        for category, message in errors:
            if message not in result[category]:
                result[category].append(message)
    def finish():
        result['unknowns'].append('reconfirm live assignments/processes and authorization at resume')
        return result
    try:
        manifest = json.loads(regular_bytes(directory / 'manifest.json'))
    except FileNotFoundError:
        result['missing'].append('manifest.json'); manifest = None
    except (json.JSONDecodeError, UnicodeError):
        result['unknowns'].append('manifest.json: invalid JSON'); manifest = None
    except (OSError, ValueError):
        result['reference_errors'].append('manifest.json: unreadable'); manifest = None
    try:
        document_hash = hashlib.sha256(regular_bytes(directory / 'HANDOFF.md')).hexdigest()
    except FileNotFoundError:
        result['missing'].append('HANDOFF.md'); document_hash = None
    except (OSError, ValueError):
        result['reference_errors'].append('HANDOFF.md: unreadable'); document_hash = None
    if isinstance(manifest, dict) and 'schema_version' not in manifest:
        result['missing'].append('manifest.schema_version')
    if not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] not in (1, 2):
        result['unknowns'].append('manifest: unrecognized version/object; original preserved')
        return finish()
    report(fields(manifest, {
        'schema_version': lambda v: type(v) is int and v in (1, 2), 'created_at': timestamp,
        'target': nonempty, 'purpose': nonempty, 'candidate': lambda v: isinstance(v, dict),
        'stable': lambda v: type(v) is bool, 'references': lambda v: isinstance(v, list),
        'records': lambda v: isinstance(v, dict), 'writers': lambda v: isinstance(v, dict),
        'unknowns': strings, 'changed_references': strings, 'sections': lambda v: isinstance(v, dict),
        'document_sha256': sha256}, 'manifest'))
    if sha256(manifest.get('document_sha256')) and document_hash and document_hash != manifest['document_sha256']:
        result['mismatches'].append('document changed after collection')
    sections = manifest.get('sections')
    report(fields(sections, {key: lambda v: type(v) is bool for key in SECTIONS}, 'manifest.sections'))
    if isinstance(sections, dict):
        result['missing'] += [key for key in SECTIONS if sections.get(key) is False]
    writers = manifest.get('writers')
    report(fields(writers, {'status': lambda v: v in ('quiescent', 'active', 'unknown'),
                           'observed_at': timestamp, 'evidence': nonempty}, 'manifest.writers'))
    if isinstance(writers, dict) and writers.get('status') != 'quiescent':
        result['unknowns'].append('writer state requires reconciliation')
    refs = manifest.get('references')
    if refs == []:
        result['missing'].append('purposeful references')
    if isinstance(refs, list):
        for index, ref in enumerate(refs):
            errors = fields(ref, {'path': relative, 'why': nonempty,
                                  'sha256': lambda v: v is None or sha256(v)}, f'manifest.references[{index}]')
            report(errors)
            if errors:
                continue
            try:
                current = reference(root, {'path': ref['path'], 'why': ref['why']})
                if current['sha256'] is None:
                    result['reference_errors'].append(ref['path'])
                elif current['sha256'] != ref['sha256']:
                    result['stale'].append(ref['path'])
            except (ValueError, OSError):
                result['reference_errors'].append(ref['path'])
    candidate = manifest.get('candidate')
    errors = candidate_errors(candidate, 'manifest.candidate'); report(errors)
    candidate_id = None
    if not errors:
        identity = candidate['identity']
        if digest(identity) != candidate['candidate']:
            result['mismatches'].append('stored candidate identity digest')
        else:
            candidate_id = candidate['candidate']
        result['unknowns'] += candidate['unknowns']
        if not candidate['stable']:
            result['mismatches'].append('candidate collection was not stable')
        try:
            current = snapshot(root, identity['watch'], identity['exclude'])
            if current['candidate'] != candidate['candidate']:
                result['stale'].append('workspace candidate')
            result['unknowns'] += current['unknowns']
            if not current['stable']:
                result['mismatches'].append('current collection was not stable')
        except (ValueError, OSError):
            result['unknowns'].append('workspace observation unavailable')
    records = manifest.get('records')
    if isinstance(records, dict):
        for path, record in records.items():
            base = 'manifest.records.' + path
            if not isinstance(record, dict):
                result['unknowns'].append(base + ': expected record object'); continue
            if set(record) == {'unrecognized_record'}:
                result['unknowns'].append(base + ': unrecognized record preserved'); continue
            if 'record_type' in record:
                if record['record_type'] != 'workflow-invocation-snapshot':
                    result['unknowns'].append(base + ': unrecognized record_type'); continue
                errors = ledger_state_errors(record, base); report(errors)
                if not errors:
                    identity = record['ledger']
                    ref = next((r for r in refs if isinstance(r, dict) and r.get('path') == identity['path']), None) if isinstance(refs, list) else None
                    if identity['path'] != path or ref is None or ref.get('sha256') != identity['sha256']:
                        result['mismatches'].append('ledger snapshot reference: ' + path)
                    for invocation, call in record['active_calls'].items():
                        if call['state'] in ('starting', 'unknown', 'running'):
                            result['unknowns'].append('active recorded assignment: ' + invocation)
                    if record['unresolved']:
                        result['unknowns'].append('ledger facts require reconciliation')
            elif set(record) == CHECK_FIELDS:
                errors = check_errors(record, base); report(errors)
                if errors:
                    continue
                error = log_error(record)
                if error:
                    category = 'mismatches' if error == 'log_mismatch' else 'reference_errors'
                    result[category].append('check log: ' + path + ': ' + error)
                if candidate_id is None:
                    result['unknowns'].append('check candidate comparison unavailable: ' + path)
                elif record['before'] != candidate_id or record['after'] != candidate_id or not record['stable']:
                    result['stale'].append('check candidate: ' + path)
            elif manifest['schema_version'] == 1 and any(k in record for k in ('limits', 'calls', 'checks', 'revision', 'usage')):
                result['unknowns'].append('historical ledger record preserved: ' + path)
                try:
                    inspection = ledger_inspect(safe_path(root, path))
                    if not inspection['valid']:
                        guidance = {
                            'convert_ledger': 'referenced ledger requires conversion: ',
                            'inspect_supported_format': 'referenced ledger requires supported format: ',
                            'use_supported_version': 'referenced ledger requires supported version: ',
                            'repair_ledger_preserving_original': 'referenced ledger is invalid: ',
                            'repair_history_preserving_original': 'referenced ledger is invalid: ',
                        }.get(inspection['next_action'], 'referenced ledger requires inspection: ')
                        result['unknowns'].append(guidance + path)
                except (LedgerError, ValueError, OSError):
                    result['unknowns'].append('referenced ledger unavailable: ' + path)
            else:
                result['unknowns'].append(base + ': unrecognized_record')
                if 'check_id' in record or 'items' in record:
                    report(check_errors(record, base))
    if strings(manifest.get('unknowns')):
        result['unknowns'] += manifest['unknowns']
    if manifest.get('stable') is False:
        result['mismatches'].append('collection was not stable')
    if strings(manifest.get('changed_references')):
        result['mismatches'] += manifest['changed_references']
    for category in result:
        result[category] = list(dict.fromkeys(result[category]))
    return finish()


BUNDLE_METHOD = ('SHA-256 over the bytes of the approval.json "bundle_files" concatenated in the listed order '
                 '(paths relative to the approval.json directory); approval.json itself is never part of the digest')


def bundle_digest(dir, files=None):
    """Compute (and, when approval.json records one, verify) the approved-bundle digest."""
    require(isinstance(dir, (str, Path)) and nonempty(str(dir)), 'Invalid dir: expected the folder holding approval.json')
    root, recorded = Path(dir), None
    if files is None:
        approval = read(safe_approval(root))
        require(isinstance(approval, dict), 'approval.json must be an object')
        files, recorded = approval.get('bundle_files'), approval.get('bundle_sha256')
    require(strings(files, non_empty=True) and bool(files), 'bundle_files must be a non-empty list of relative file paths')
    hasher = hashlib.sha256()
    for name in files:
        hasher.update(regular_bytes(safe_path(root, name)))
    digest_value = hasher.hexdigest()
    return {'digest': digest_value, 'recorded': recorded, 'valid': recorded is None or recorded == digest_value,
            'files': files, 'method': BUNDLE_METHOD}


def safe_approval(root):
    path = root / 'approval.json'
    require(path.is_file(), 'No approval.json in ' + str(root) + '; pass "files" to compute a digest')
    return path


def plain_call(action, data, output=None, input_path=None):
    """Run one non-ledger action on a decoded request; the full result also goes to `output` (create-only)."""
    require(isinstance(data, dict), 'Request must be an object')
    if output is not None:
        command_output_preflight(output, data, action, input_path)
    if action == 'snapshot':
        result = snapshot(**data)
    elif action == 'audit':
        result = audit(read(data['before']), read(data['after']), data['allowed'])
    elif action == 'restore':
        result = restore(**data)
    elif action == 'plan':
        result = validate_plan(read(data['plan']), read(data['requirements']))
    elif action == 'check':
        data['approved'] = read(data.pop('requirements'))
        result = run_check(**data)
    elif action == 'gate':
        require(strings(data['checks'], non_empty=True), 'Invalid checks: expected a non-empty list of check record paths')
        result = validate_gate(read(data['requirements']), data['candidate'], [read(p) for p in data['checks']])
        for issue in result['issues']:
            if 'check_index' in issue:
                issue['check_path'] = data['checks'][issue['check_index']]
    elif action == 'bundle-digest':
        result = bundle_digest(**data)
    elif action == 'handoff':
        result = handoff_create(**data)
    else:
        result = handoff_validate(**data)
    if output is not None:
        exclusive_json(output, result)
    return result


def plain_code(action, result):
    if result.get('valid') is False or result.get('passed') is False or \
            (action == 'restore' and result.get('status') == 'blocked'):
        return 1
    return 0


SUMMARY_LIMIT = 20


def capped(result, fields, limit=SUMMARY_LIMIT):
    """Copy list fields truncated to `limit`, with true totals in `counts`."""
    out, counts = {}, {}
    for name in fields:
        values = result.get(name)
        if isinstance(values, list):
            counts[name] = len(values)
            out[name] = values[:limit]
    if any(total > limit for total in counts.values()):
        out['truncated'] = sorted(name for name, total in counts.items() if total > limit)
    out['counts'] = counts
    return out


def summarize(action, result, output=None, limit=SUMMARY_LIMIT):
    """Compact stdout summary of a non-ledger result; never blobs or per-file identity maps.

    The complete result exists only in the `--output` file or when `--full` is given.
    """
    saved = str(Path(output).resolve()) if output is not None else 'not saved; use --output NEW_FILE or --full'
    head = {'action': action, 'full_result': saved}
    if action == 'snapshot':
        identity = result['identity']
        return {**head, 'candidate': result['candidate'], 'stable': result['stable'],
                'observed_at': result['observed_at'], 'workspace': result['workspace'],
                'head': identity['head'], 'checkpoint': 'blobs' in result,
                'file_count': len(identity['files']), 'index_count': len(identity['index']),
                'blob_count': len(result.get('blobs', ())), **capped(result, ['unknowns'], limit)}
    if action == 'audit':
        return {**head, 'comparable': result['comparable'],
                **capped(result, ['changed', 'index_changed', 'outside_scope'], limit)}
    if action == 'restore':
        kept = {k: result[k] for k in ('mode', 'status', 'workspace', 'pre_candidate', 'post_candidate',
                                       'current_candidate', 'matches_pre_delta', 'restored_candidate') if k in result}
        summary = {**head, **kept, **capped(result, ['unknowns', 'delta', 'index_delta', 'conflicts', 'actions',
                                                     'restored_unknowns'], limit)}
        kinds = {}
        for entry in result.get('actions', ()):
            kinds[entry['action']] = kinds.get(entry['action'], 0) + 1
        summary['action_kinds'] = kinds
        return summary
    if action == 'bundle-digest':
        return {**head, **{k: v for k, v in result.items() if k != 'method'}}
    if action == 'plan':
        return {**head, 'valid': result['valid'], 'requirements_identity': result['requirements_identity'],
                **capped(result, ['errors'], limit)}
    if action == 'check':
        return {**head, **{k: result[k] for k in ('check_id', 'exit_code', 'timed_out', 'stable', 'before', 'after',
                                                  'log', 'log_sha256', 'cwd', 'started_at', 'finished_at')}}
    if action == 'gate':
        return {**head, 'passed': result['passed'], **capped(result, ['issues'], limit)}
    if action == 'handoff':
        return {**head, 'path': result['path'], 'stable': result['stable'], 'starter': result['starter'],
                'validation': capped(result['validation'], list(result['validation']), limit)}
    return {**head, **capped(result, list(result), limit)}  # handoff-validate


CONTRACT_NAME = 'workflow-contract.md'


def parse_number_list(text):
    numbers = []
    for part in str(text).split(','):
        low, dash, high = part.strip().partition('-')
        require(low.isdigit() and (not dash or high.isdigit()), 'Sections must look like 1,3,5-9')
        numbers.extend(range(int(low), int(high or low) + 1))
    return numbers


def find_contract(explicit=None):
    if explicit:
        return Path(explicit)
    for base in (Path.cwd(), *Path.cwd().parents):
        if (base / '.codex' / CONTRACT_NAME).is_file():
            return base / '.codex' / CONTRACT_NAME
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    require((home / CONTRACT_NAME).is_file(), 'Installed workflow contract not found; pass --path')
    return home / CONTRACT_NAME


def contract_sections(text, wanted):
    """Return the requested numbered `## N.` sections in contract order."""
    parts = re.split(r'(?m)^(?=## \d+\. )', text)
    found = {int(re.match(r'## (\d+)\.', part).group(1)): part.rstrip() + '\n' for part in parts[1:]}
    missing = [n for n in wanted if n not in found]
    require(not missing, 'Unknown contract sections: ' + ','.join(map(str, missing)))
    return '\n'.join(found[n] for n in sorted(set(wanted)))


def formats_text():
    return (Path(__file__).resolve().parents[1] / 'references' / 'formats.md').read_text(encoding='utf-8')


FORMAT_RELATED = {'requirements', 'check', 'check-items', 'gate', 'event-check'}
FORMAT_ALIASES = {'batch': ('batch-setup', 'batch-dispatch', 'batch-result', 'batch-pre', 'batch-post')}


def format_actions(text, actions):
    """Request-example lines of the formats quick reference; returns (text, unknown action names)."""
    block = text.split('## Common actions (quick reference)')[1].split('```')[1]
    known = sorted({line.split(' ', 1)[0] for line in block.strip().splitlines()} | set(FORMAT_ALIASES))
    matched = [a for a in actions if a in known]
    require(matched, 'Unknown action; known: ' + ', '.join(known))
    wanted = set(matched)
    for name in matched:
        wanted.update(FORMAT_ALIASES.get(name, ()))
    # A check/gate request needs its requirements file, items record and ledger event too.
    if wanted & {'check', 'gate'}:
        wanted |= FORMAT_RELATED
    lines = [line for line in block.strip().splitlines() if line.split(' ', 1)[0] in wanted]
    return '\n'.join(lines) + '\n', [a for a in actions if a not in known]


def format_heading(text, heading):
    """A formats.md section whose heading contains `heading`, up to the next heading of equal or higher level."""
    lines = text.splitlines()
    for start, line in enumerate(lines):
        match = re.match(r'(#+) (.*)', line)
        if match and heading.lower() in match.group(2).lower():
            level = len(match.group(1))
            end = next((i for i in range(start + 1, len(lines))
                        if re.match(r'#{1,%d} ' % level, lines[i])), len(lines))
            return '\n'.join(lines[start:end]).rstrip() + '\n'
    raise ValueError('No formats heading contains: ' + heading)


BRIEFS = {
    'implement': {'contract': '1,3-9,11',
                  'files': ('workflow-ledger/SKILL.md', 'workspace-evidence/SKILL.md', 'change-verification/SKILL.md',
                            'implement/references/dispatch-and-oracle.md', 'implement/references/progress-report.md'),
                  'actions': ('batch', 'plan', 'bundle-digest', 'ledger-init', 'ledger-update', 'snapshot', 'audit', 'check', 'gate')},
    'quick-change': {'contract': '6-9,11',
                     'files': ('quick-change/references/change-template.md', 'workflow-ledger/SKILL.md',
                               'workspace-evidence/SKILL.md', 'change-verification/SKILL.md',
                               'implement/references/dispatch-and-oracle.md', 'implement/references/progress-report.md'),
                     'actions': ('batch', 'ledger-init', 'ledger-update', 'snapshot', 'audit')},
}


def brief_text(name, contract_path=None):
    """Everything a skill tells Root to read at the start, printed in ONE command (one turn instead of several)."""
    require(name in BRIEFS, 'Unknown brief; use one of: ' + ', '.join(BRIEFS))
    spec, skills = BRIEFS[name], Path(__file__).resolve().parents[2]
    parts = []
    for relative in spec['files']:
        parts.append(f'===== {relative} =====\n' + (skills / relative).read_text(encoding='utf-8').rstrip() + '\n')
    contract = find_contract(contract_path).read_text(encoding='utf-8')
    parts.append(f'===== contract sections {spec["contract"]} =====\n' + contract_sections(contract, parse_number_list(spec['contract'])))
    formats = formats_text()
    quick = formats.split('## Common actions (quick reference)')[1].split('\n```')[0]
    lines, _ = format_actions(formats, list(spec['actions']))
    parts.append('===== formats quick reference =====\n' + quick.split('\n\n', 2)[1].strip() + '\n\n' + lines)
    return '\n'.join(parts)


def reference_main(args):
    try:
        if args.action == 'brief':
            require(args.for_skill, 'Use --for, for example --for implement')
            sys.stdout.write(brief_text(args.for_skill, args.path))
        elif args.action == 'contract':
            require(args.sections, 'Use --sections, for example --sections 1,3,7-9')
            text = find_contract(args.path).read_text(encoding='utf-8')
            sys.stdout.write(contract_sections(text, parse_number_list(args.sections)))
        else:
            require(bool(args.actions) != bool(args.heading), 'Use exactly one of --actions or --heading')
            text = formats_text()
            if args.actions:
                lines, unknown = format_actions(text, [a.strip() for a in args.actions.split(',')])
                sys.stdout.write(lines)
                if unknown:
                    print(json.dumps({'warning': 'No quick-reference example for: ' + ', '.join(unknown)}), file=sys.stderr)
            else:
                sys.stdout.write(format_heading(text, args.heading))
        return 0
    except (ValueError, KeyError, IndexError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


BATCH_ACTIONS = ('ledger-init', 'ledger-read', 'ledger-inspect', 'ledger-update', 'reconcile',
                 'snapshot', 'audit', 'plan', 'check', 'gate', 'bundle-digest')
BATCH_FIELDS = {'steps', 'stop_on_error'}
BATCH_STEP_LIMIT = 30


def brief(action, result):
    """Smallest per-step summary for a batch; the full result stays in the step's `output` file."""
    if action in LEDGER_ACTIONS:
        kept = {k: result[k] for k in ('run_id', 'revision', 'next_action', 'usage_known', 'mutation',
                                       'detected_format', 'valid', 'recognized') if k in result}
        if isinstance(result.get('active_calls'), dict):
            kept['active_calls'] = {name: call['state'] for name, call in result['active_calls'].items()}
        if action == 'reconcile':
            kept.update(mismatches=result['mismatches'][:SUMMARY_LIMIT], unrecorded=result['unrecorded'][:SUMMARY_LIMIT])
        return kept
    full = summarize(action, result)
    for noisy in ('full_result', 'action', 'workspace', 'observed_at', 'head', 'cwd', 'started_at', 'finished_at', 'log'):
        full.pop(noisy, None)
    if 'truncated' not in full:
        full.pop('counts', None)  # totals only matter when a list was capped
    if not full.get('checkpoint', True):
        full.pop('blob_count', None)
    return {k: v for k, v in full.items() if v != []}


def batch_error(index, action, code, detail, cause=None):
    error = {'code': 'batch_step_failed', 'step': index, 'action': action, 'exit_code': code,
             'json_path': f'$.steps[{index}]', 'detail': detail}
    if cause:
        inner = cause.get('error', cause) if isinstance(cause, dict) else {}
        if isinstance(inner, dict):
            error['cause'] = inner
            if isinstance(inner.get('json_path'), str) and inner['json_path'].startswith('$'):
                error['json_path'] = f'$.steps[{index}].request' + inner['json_path'][1:]
        elif isinstance(inner, str):
            error['cause'] = {'message': inner}
    return {'error': error}


def batch_main(input_path, full=False):
    """Run several helper actions in order. Each step behaves exactly like the individual command
    (same validation, atomicity and --output rules); steps that finished before a failure keep their effects."""
    steps, results = [], []
    try:
        try:
            # A heredoc or pipe (`--input /dev/stdin`) lets Root write and run a phase in one command.
            raw = Path(input_path).read_bytes() if ledger_request_stream(input_path) else ledger_source(Path(input_path))[0]
            plan = ledger_decode(raw)
        except (OSError, LedgerError):
            raise ValueError('Cannot read the batch request as strict JSON') from None
        require(isinstance(plan, dict) and set(plan) <= BATCH_FIELDS and 'steps' in plan, 'Batch request needs "steps" (and optional "stop_on_error")')
        steps, stop = plan['steps'], plan.get('stop_on_error', True)
        require(type(stop) is bool, 'stop_on_error must be a boolean')
        require(isinstance(steps, list) and 0 < len(steps) <= BATCH_STEP_LIMIT, f'steps must be a list of 1-{BATCH_STEP_LIMIT} steps')
        for index, step in enumerate(steps):
            require(isinstance(step, dict) and set(step) <= {'action', 'request', 'output'} and {'action', 'request'} <= set(step),
                    f'steps[{index}] needs exactly action, request and optional output')
            require(step['action'] in BATCH_ACTIONS, f'steps[{index}].action must be one of: ' + ', '.join(BATCH_ACTIONS)
                    + ' (batch, restore, ledger-convert, handoff, contract and formats stay standalone; git is never part of a batch)')
            require(isinstance(step['request'], dict), f'steps[{index}].request must be an inline object')
            require(step.get('output') is None or isinstance(step['output'], str), f'steps[{index}].output must be a path')
    except ValueError as exc:
        print(json.dumps({'error': {'code': 'invalid_batch', 'json_path': '$', 'detail': str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    # Pure request validation of every ledger step first: a typo must not leave earlier steps applied.
    for index, step in enumerate(steps):
        if step['action'] in LEDGER_ACTIONS:
            try:
                ledger_request(step['action'], copy.deepcopy(step['request']))
            except LedgerError as exc:
                failure = batch_error(index, step['action'], 2, 'request rejected before any step ran',
                                      ledger_error_json(exc, step['action'], step['request']))
                print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
                return 2
    code, failure = 0, None
    for index, step in enumerate(steps):
        action, output = step['action'], step.get('output')
        entry = {'index': index, 'action': action}
        if failure is not None and stop:
            results.append({**entry, 'status': 'skipped'})
            continue
        request = copy.deepcopy(step['request'])
        step_code, result, error = 0, None, None
        if action in LEDGER_ACTIONS:
            box = {}
            step_code = ledger_main(action, input_path, output, request=request, collect=box)
            result, error = box.get('result'), box.get('error')
        else:
            try:
                result = plain_call(action, request, output, input_path)
                step_code = plain_code(action, result)
            except KeyError as exc:
                error = {'error': 'Missing request field: ' + str(exc.args[0]) + ' (see formats --actions ' + action + ')'}
                step_code = 2
            except (ValueError, TypeError, OSError) as exc:
                error, step_code = {'error': str(exc)}, 2
        if step_code == 0 and error is None:
            results.append({**entry, 'status': 'ok', 'summary': brief(action, result),
                            **({'output': str(Path(output).resolve())} if output else {}),
                            **({'result': result} if full else {})})
            continue
        status = 'error' if error is not None else 'failed'
        results.append({**entry, 'status': status, 'exit_code': step_code,
                        **({'summary': brief(action, result)} if result is not None else {}),
                        **({'output': str(Path(output).resolve())} if output and result is not None else {})})
        code = max(code, step_code)
        if failure is None:
            failure = batch_error(index, action, step_code, 'step returned an error' if error is not None else
                                  'step reported a failed result (exit 1); see its summary', error)
            if not stop:
                continue
    ran = [r for r in results if r['status'] != 'skipped']
    print(json.dumps({'action': 'batch', 'status': 'ok' if failure is None else 'failed', 'stop_on_error': stop,
                      'counts': {'steps': len(steps), 'ok': sum(r['status'] == 'ok' for r in results), 'ran': len(ran)},
                      'steps': results}, ensure_ascii=False, separators=(',', ':')))
    if failure is not None:
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
    return code


def main(default=None):
    parser = argparse.ArgumentParser(description=__doc__)
    if default is None:
        parser.add_argument('action', choices=list(ACTIONS) + ['batch', 'brief', 'contract', 'formats'])
    parser.add_argument('--input', help='JSON request; see workflow-ledger/references/formats.md')
    parser.add_argument('--output', help='Optional create-only command result file holding the full result; not a ledger journal. Use a fresh path or omit this option')
    parser.add_argument('--full', action='store_true', help='Print the complete result to stdout instead of the compact summary (snapshot checkpoints include every file byte)')
    if default is None:
        parser.add_argument('--for', dest='for_skill', help='brief: implement or quick-change; prints the skill files, contract sections and request examples it needs in one go')
        parser.add_argument('--sections', help='contract: section numbers to print, for example 1,3,7-9')
        parser.add_argument('--path', help='contract: explicit path of the installed workflow-contract.md')
        parser.add_argument('--actions', help='formats: comma-separated action names whose request example to print')
        parser.add_argument('--heading', help='formats: print the section whose heading contains this text')
    args = parser.parse_args()
    if default is None and args.action in ('brief', 'contract', 'formats'):
        return reference_main(args)
    if not args.input:
        parser.error('--input is required')
    action = default or args.action
    if action == 'batch':
        if args.output:
            parser.error('batch takes no --output; give each step its own "output"')
        return batch_main(args.input, args.full)
    if action in LEDGER_ACTIONS:
        return ledger_main(action, args.input, args.output)
    try:
        data = read(args.input)
        result = plain_call(action, data, args.output, args.input)
        print(json.dumps(result if args.full else summarize(action, result, args.output), ensure_ascii=False, indent=2))
        return plain_code(action, result)
    except KeyError as exc:
        print(json.dumps({'error': 'Missing request field: ' + str(exc.args[0]) + ' (see formats --actions ' + action + ')'},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, TypeError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
