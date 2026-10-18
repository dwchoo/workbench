"""Explicit, local cleanup of regenerable workflow caches and old checkpoints."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone


def checksum(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(data)
    return value.hexdigest()


def identity(path):
    s = path.lstat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def in_use(workflow):
    """Inspect same-UID processes; inaccessible same-UID state blocks cleanup."""
    paths = set()
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            targets = []
            for name in ('cwd', 'exe'):
                try:
                    targets.append(os.readlink(process / name))
                except FileNotFoundError:
                    pass
            for fd in (process / 'fd').iterdir():
                try:
                    targets.append(os.readlink(fd))
                except FileNotFoundError:
                    pass
            targets.extend(line.split()[-1] for line in (process / 'maps').read_text().splitlines()
                           if str(workflow) + '/' in line)
            targets.extend((process / 'cmdline').read_bytes().decode(errors='replace').split('\0'))
            for target in targets:
                target = target.removesuffix(' (deleted)')
                if target.startswith(str(workflow) + '/'):
                    paths.add(target)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            # Privilege-separated login/session daemons are not fixture owners.
            # Their /proc state is intentionally protected even from the same UID.
            command = (process / 'cmdline').read_bytes().decode(errors='replace')
            if (command.startswith('/usr/lib/systemd/systemd\0')
                    or command.startswith('(sd-pam)') or command.startswith('sshd: ')):
                continue
            raise RuntimeError('Cannot establish same-UID process quiescence') from exc
    return paths


def busy(path, used):
    return any(str(path) == p or str(path).startswith(p.rstrip('/') + '/') for p in used)


def cache_file(path):
    s = path.as_posix()
    return bool(re.search(r'/private/(?:[^/]+/)*\.omp/natives/[^/]+/pi_natives\.[^/]+\.node$', s)
                or re.search(r'/private/(?:[^/]+/)*(?:agent|\.omp)/models\.db(?:-wal|-shm)?$', s))


def checkpoint_file(path):
    if path.suffix != '.json' or path.stat().st_size < 10 * 1024 * 1024:
        return False
    with path.open('rb') as stream:
        return stream.read(32).startswith(b'{"blobs":{')


def archive(path, record):
    destination = Path(str(path) + '.gz')
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('Archive destination already exists: ' + str(destination))
    before = identity(path)
    expected = checksum(path)
    fd, name = tempfile.mkstemp(prefix='.checkpoint-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            subprocess.run(['pigz', '-1', '-n', '-p', '4', '-c', '--', str(path)],
                           stdout=stream, check=True)
            stream.flush()
            os.fsync(stream.fileno())
        actual = hashlib.sha256()
        with gzip.open(temporary, 'rb') as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b''):
                actual.update(data)
        if actual.hexdigest() != expected or identity(path) != before:
            raise RuntimeError('Checkpoint changed or archive verification failed: ' + str(path))
        shutil.copystat(path, temporary)
        os.replace(temporary, destination)
        record({'action': 'archive', 'original': str(path), 'archive': str(destination),
                'original_sha256': expected, 'archive_sha256': checksum(destination),
                'original_bytes': before[2], 'archive_bytes': destination.stat().st_size})
        path.unlink()
        return before[2] - destination.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


def restore(path, workflow, log):
    if path.exists() or path.is_symlink() or not path.resolve().is_relative_to(workflow):
        raise RuntimeError('Restore requires an absent path inside .workflow')
    proofs = []
    for journal in (workflow / 'storage-maintenance').glob('*/actions.jsonl'):
        for line in journal.read_text().splitlines():
            item = json.loads(line)
            if item.get('action') == 'archive' and item['original'] == str(path):
                proofs.append(item)
    if len(proofs) != 1:
        raise RuntimeError('Expected exactly one archive proof')
    proof = proofs[0]
    source = Path(proof['archive'])
    if not source.resolve().is_relative_to(workflow) or source.is_symlink():
        raise RuntimeError('Unsafe archive path')
    if checksum(source) != proof['archive_sha256']:
        raise RuntimeError('Archive checksum mismatch')
    fd, name = tempfile.mkstemp(prefix='.restore-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output, gzip.open(source, 'rb') as stream:
            shutil.copyfileobj(stream, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if checksum(temporary) != proof['original_sha256']:
            raise RuntimeError('Original checksum mismatch')
        shutil.copystat(source, temporary)
        # Create-only install; never overwrite a concurrently restored file.
        os.link(temporary, path)
        log({'action': 'restore', 'original': str(path), 'sha256': proof['original_sha256']})
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Otherwise report candidates only')
    parser.add_argument('--archive-checkpoints', action='store_true')
    parser.add_argument('--restore', type=Path)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    if not (root / 'AGENTS.md').is_file() or not (root / '.git').exists():
        raise RuntimeError('Run from repository root')
    workflow = root / '.workflow'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    report = workflow / 'storage-maintenance' / stamp
    used = in_use(workflow)
    if args.apply:
        report.mkdir(parents=True, mode=0o700)
    def log(item):
        if args.apply:
            with (report / 'actions.jsonl').open('a') as stream:
                stream.write(json.dumps(item, ensure_ascii=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
    if args.restore:
        if not args.apply:
            raise RuntimeError('Restore requires --apply')
        path = args.restore.absolute()
        if busy(path, used):
            raise RuntimeError('Restore target is in use')
        restore(path, workflow, log)
        print(json.dumps({'restored': str(path), 'report': str(report)}))
        return
    candidates = []
    for folder, dirs, names in os.walk(workflow, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink()
                   and not (Path(folder) / d).is_relative_to(workflow / 'storage-maintenance')]
        for name in names:
            path = Path(folder) / name
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                continue
            kind = ('cache' if cache_file(path) else
                    'checkpoint' if args.archive_checkpoints and checkpoint_file(path) else None)
            if kind:
                candidates.append((kind, path))
    skipped = []
    counts = {'cache': 0, 'checkpoint': 0}
    saved = 0
    for kind, path in candidates:
        if busy(path, used):
            skipped.append(str(path))
            continue
        if args.apply:
            # Recheck process ownership before each potentially destructive operation.
            if busy(path, in_use(workflow)):
                skipped.append(str(path))
                continue
            if kind == 'cache':
                before = identity(path)
                sha = checksum(path)
                if identity(path) != before:
                    raise RuntimeError('Cache changed during inspection')
                log({'action': 'remove_regenerable_cache', 'path': str(path),
                     'bytes': before[2], 'sha256': sha})
                path.unlink()
                saved += before[2]
            else:
                saved += archive(path, log)
        counts[kind] += 1
        if args.apply and sum(counts.values()) % 20 == 0:
            print(json.dumps({'progress': counts, 'saved_bytes': saved}), flush=True)
    result = {'applied': args.apply, 'counts': counts, 'saved_bytes': saved,
              'skipped_in_use': skipped, 'report': str(report) if args.apply else None}
    if args.apply:
        (report / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
