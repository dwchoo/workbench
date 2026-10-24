"""Registry API and bytes (HB-04, seam S3): ``workbench.hub.registry`` in a temp state dir.

Expected values are the test's own paths and uuids and the SPEC "Registry 계약" schema; file
bytes and modes are read with plain ``json``/``os`` calls, not through the product reader.
Concurrent writers are real subprocesses. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import registry  # noqa: E402
from workbench.hub.paths import HubLayout  # noqa: E402


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-registry-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.layout = HubLayout(self.base / 'state' / 'omp-workbench' / 'hub')
        self.path = self.layout.state_dir / 'registry.json'

    def root(self, name: str) -> Path:
        path = self.base / 'projects' / name
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def disk(self) -> dict:
        return json.loads(self.path.read_text())


class EnsureCreatesTests(RegistryCase):
    def test_ensure_on_empty_state_creates_private_file_with_schema(self):
        root, project_id = self.root('alpha'), str(uuid.uuid4())
        result = registry.ensure(self.layout, root, project_id, 'init')
        self.assertTrue(result.added)
        info = os.lstat(self.path)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        value = self.disk()
        self.assertEqual(value['schema_version'], 1)
        self.assertEqual(len(value['projects']), 1)
        entry = value['projects'][0]
        self.assertEqual({key: entry[key] for key in ('root', 'project_id', 'name', 'source')},
                         {'root': str(root), 'project_id': project_id, 'name': 'alpha', 'source': 'init'})
        self.assertIsInstance(entry['registered_at'], (int, float))
        self.assertEqual(set(entry), {'root', 'project_id', 'name', 'registered_at', 'source'})


class EnsureUniquenessTests(RegistryCase):
    def test_same_root_twice_keeps_one_entry_and_original_bytes(self):
        root, project_id = self.root('alpha'), str(uuid.uuid4())
        registry.ensure(self.layout, root, project_id, 'start')
        before = self.path.read_bytes()
        result = registry.ensure(self.layout, root, project_id, 'add')
        self.assertFalse(result.added)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.disk()['projects'][0]['source'], 'start')

    def test_same_root_with_another_id_is_mismatch_and_not_overwritten(self):
        root, project_id = self.root('alpha'), str(uuid.uuid4())
        registry.ensure(self.layout, root, project_id, 'start')
        before = self.path.read_bytes()
        with self.assertRaises(registry.RegistryMismatch) as raised:
            registry.ensure(self.layout, root, str(uuid.uuid4()), 'start')
        self.assertEqual(raised.exception.reason, 'registry_mismatch')
        self.assertEqual(self.path.read_bytes(), before)

    def test_same_id_under_another_root_is_rejected(self):
        project_id = str(uuid.uuid4())
        registry.ensure(self.layout, self.root('alpha'), project_id, 'start')
        before = self.path.read_bytes()
        with self.assertRaises(registry.RegistryError) as raised:
            registry.ensure(self.layout, self.root('beta'), project_id, 'add')
        self.assertEqual(raised.exception.reason, 'registry_id_conflict')
        self.assertEqual(self.path.read_bytes(), before)

    def test_different_roots_and_ids_coexist_in_registration_order(self):
        ids = [str(uuid.uuid4()) for _ in range(2)]
        registry.ensure(self.layout, self.root('alpha'), ids[0], 'init')
        registry.ensure(self.layout, self.root('beta'), ids[1], 'add')
        self.assertEqual([e['project_id'] for e in self.disk()['projects']], ids)
        self.assertEqual([e.project_id for e in registry.read(self.layout)], ids)


class RemoveTests(RegistryCase):
    def test_remove_drops_only_that_entry_and_reports_missing_as_none(self):
        ids = [str(uuid.uuid4()) for _ in range(2)]
        alpha, beta = self.root('alpha'), self.root('beta')
        registry.ensure(self.layout, alpha, ids[0], 'init')
        registry.ensure(self.layout, beta, ids[1], 'add')
        removed = registry.remove(self.layout, alpha)
        self.assertEqual((removed.root, removed.project_id), (str(alpha), ids[0]))
        self.assertEqual([e['root'] for e in self.disk()['projects']], [str(beta)])
        self.assertIsNone(registry.remove(self.layout, alpha))
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)

    def test_read_of_absent_registry_is_empty_without_creating_state(self):
        self.assertEqual(registry.read(self.layout), [])
        self.assertFalse(os.path.lexists(self.layout.state_dir))


def entry_json(root: str, project_id: str | None = None, **changes) -> dict:
    value = {'root': root, 'project_id': project_id or str(uuid.uuid4()), 'name': Path(root).name,
             'registered_at': 1.0, 'source': 'add'}
    value.update(changes)
    return value


class DamagedRegistryTests(RegistryCase):
    """Reads report ``registry_invalid``; writers refuse and leave the bytes and mode alone."""

    def damaged_variants(self):
        good = str(self.root('good'))
        wrapped = lambda *projects, **top: {'schema_version': 1, 'projects': list(projects), **top}  # noqa: E731
        shared_id = '8b5c1b2e-0a53-4d49-8d0e-0f4e2f0f6a11'
        return {
            'not json': (b'{"schema_version": 1, "projects": [', 0o600),
            'not an object': (b'[]', 0o600),
            'wrong schema_version': (json.dumps(wrapped(schema_version=2)).encode(), 0o600),
            'projects not a list': (json.dumps({'schema_version': 1, 'projects': {}}).encode(), 0o600),
            'entry missing key': (json.dumps(wrapped({'root': good})).encode(), 0o600),
            'relative root': (json.dumps(wrapped(entry_json('relative/dir'))).encode(), 0o600),
            'unknown source': (json.dumps(wrapped(entry_json(good, source='web'))).encode(), 0o600),
            'bad project_id': (json.dumps(wrapped(entry_json(good, 'not-a-uuid'))).encode(), 0o600),
            'duplicate root': (json.dumps(wrapped(entry_json(good), entry_json(good))).encode(), 0o600),
            'duplicate project_id': (json.dumps(wrapped(
                entry_json(good, shared_id), entry_json(str(self.root('other')), shared_id))).encode(), 0o600),
            'mode 0644': (json.dumps(wrapped()).encode(), 0o644),
        }

    def assert_refused_and_untouched(self, before: bytes, mode: int):
        other_root, other_id = self.root('new'), str(uuid.uuid4())
        for call in (lambda: registry.read(self.layout),
                     lambda: registry.ensure(self.layout, other_root, other_id, 'add'),
                     lambda: registry.remove(self.layout, other_root)):
            with self.assertRaises(registry.RegistryError) as raised:
                call()
            self.assertEqual(raised.exception.reason, 'registry_invalid')
            self.assertIn(str(self.path), raised.exception.detail)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), mode)
        self.assertEqual(sorted(p.name for p in self.layout.state_dir.iterdir() if p.name != 'registry.lock'),
                         ['registry.json'])

    def test_damaged_content_or_mode_is_invalid_for_read_and_untouched_by_writers(self):
        for label, (payload, mode) in self.damaged_variants().items():
            with self.subTest(label):
                if self.layout.state_dir.exists():
                    for child in self.layout.state_dir.iterdir():
                        child.unlink()
                self.layout.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.path.write_bytes(payload)
                os.chmod(self.path, mode)
                self.assert_refused_and_untouched(payload, mode)

    def test_symlinked_registry_file_is_refused_and_target_untouched(self):
        self.layout.state_dir.mkdir(parents=True, mode=0o700)
        target = self.base / 'elsewhere.json'
        target.write_text('{"schema_version": 1, "projects": []}')
        os.chmod(target, 0o600)
        self.path.symlink_to(target)
        for call in (lambda: registry.read(self.layout),
                     lambda: registry.ensure(self.layout, self.root('a'), str(uuid.uuid4()), 'add')):
            with self.assertRaises(registry.RegistryError) as raised:
                call()
            self.assertEqual(raised.exception.reason, 'registry_invalid')
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), '{"schema_version": 1, "projects": []}')

    def test_symlinked_state_directory_is_refused(self):
        real = self.base / 'real-state'
        real.mkdir(mode=0o700)
        (real / 'registry.json').write_text('{"schema_version": 1, "projects": []}')
        os.chmod(real / 'registry.json', 0o600)
        self.layout.state_dir.parent.mkdir(parents=True, mode=0o700)
        self.layout.state_dir.symlink_to(real, target_is_directory=True)
        with self.assertRaises(registry.RegistryError):
            registry.read(self.layout)
        with self.assertRaises(registry.RegistryError):
            registry.ensure(self.layout, self.root('a'), str(uuid.uuid4()), 'add')
        self.assertEqual(sorted(p.name for p in real.iterdir()), ['registry.json'])


WRITER = '''
import sys, time, uuid
from pathlib import Path
from workbench.hub import registry
from workbench.hub.paths import HubLayout
layout, gate, tag, count = HubLayout(Path(sys.argv[1])), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
while not gate.exists():
    time.sleep(0.001)
for number in range(count):
    root = Path(sys.argv[5]) / f"{tag}-{number}"
    root.mkdir(parents=True, exist_ok=True)
    registry.ensure(layout, root, str(uuid.uuid4()), "add")
'''

HOLDER = '''
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(60)
'''


class ConcurrentWriterTests(RegistryCase):
    def env(self):
        return {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}

    def test_four_processes_writing_different_roots_lose_nothing(self):
        gate = self.base / 'go'
        projects = self.base / 'projects'
        children = [subprocess.Popen([sys.executable, '-c', WRITER, str(self.layout.state_dir), str(gate),
                                      f'p{index}', '8', str(projects)], env=self.env(),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for index in range(4)]
        self.addCleanup(lambda: [child.kill() for child in children if child.poll() is None])
        gate.touch()
        for child in children:
            _out, err = child.communicate(timeout=60)
            self.assertEqual(child.returncode, 0, err)
        roots = sorted(Path(entry['root']).name for entry in self.disk()['projects'])
        self.assertEqual(roots, sorted(f'p{i}-{n}' for i in range(4) for n in range(8)))
        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)

    def test_held_registry_lock_makes_writers_report_busy_without_changes(self):
        registry.ensure(self.layout, self.root('alpha'), str(uuid.uuid4()), 'init')
        before = self.path.read_bytes()
        holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(self.layout.state_dir / 'registry.lock')],
                                  env=self.env(), stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        for call in (lambda: registry.ensure(self.layout, self.root('beta'), str(uuid.uuid4()), 'add',
                                             lock_timeout=0.3),
                     lambda: registry.remove(self.layout, self.root('alpha'), lock_timeout=0.3)):
            with self.assertRaises(registry.RegistryError) as raised:
                call()
            self.assertEqual(raised.exception.reason, 'registry_busy')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(registry.read(self.layout)), 1)  # readers never wait for the lock


if __name__ == '__main__':
    unittest.main()
