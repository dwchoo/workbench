"""Independent verification of the Hub auth core (TI-02, seams T1a library+files and T8 CLI).

Scenarios come from SPEC "인증·세션·CSRF", "감사 기록", "API key", "CLI 추가", BRIEF D-10..D-12/D-30,
P2-AC-17..19 and ADR-0003, not from the worker's tests. Expected values are SPEC numbers,
hashes this module computes with ``hashlib.scrypt``/``hashlib.sha256``, and raw bytes of the
files on disk (stdlib ``sqlite3``/``json``/``os``). Every file and directory lives in a temp
dir this module owns; subprocesses run in a temp HOME/XDG sandbox with Hub auto-start off
(the one test that proves "no Hub is started" turns it on).

``PYTHONPATH`` is honoured (resolved to absolute entries) for the library and the CLI
subprocesses, so the same file can be pointed at a mutated copy of ``src``.

Run with ``discover -s tests/hub -p 'test_hub_auth_independent.py'``.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import hmac
import inspect
import io
import json
import logging
import os
from pathlib import Path
import pty
import re
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if not os.environ.get('PYTHONPATH'):
    sys.path.insert(0, str(REPO / 'src'))

from workbench.backend.paths import DataDirError  # noqa: E402
from workbench.hub import auth  # noqa: E402
from test_hub_spawn import SpawnSandbox  # noqa: E402

PASSWORD = 'Zx9-unique-passphrase-ALPHA'
OTHER_PASSWORD = 'Qw3-another-passphrase-BETA'
T0 = 1_800_000_000.0
DAY = 86400.0


def absolute_pythonpath() -> str:
    entries = [str(Path(item).resolve()) for item in os.environ.get('PYTHONPATH', '').split(os.pathsep) if item]
    return os.pathsep.join(entries) if entries else str(REPO / 'src')


def scrypt_bytes(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=1, dklen=32,
                          maxmem=64 * 1024 * 1024)


def mode_of(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def f_of(failures: int) -> int:
    """SPEC f(n), written independently: 0 for n <= 3 else min(2^(n-4), 30)."""
    return 0 if failures <= 3 else min(2 ** (failures - 4), 30)


def needles_for(secret: str) -> list[bytes]:
    raw = secret.encode('utf-8')
    return [raw, base64.b64encode(raw), base64.urlsafe_b64encode(raw), base64.b64encode(raw).rstrip(b'='),
            raw.hex().encode(), raw.hex().upper().encode()]


class AuthCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-authi-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / 'state' / 'omp-workbench' / 'hub'
        self.auth_path = self.state / 'auth.json'
        self.db_path = self.state / 'hub.sqlite3'
        self._dbs: list = []

    # -- builders ---------------------------------------------------------------------------
    def passwords(self, clock=lambda: T0) -> auth.PasswordStore:
        return auth.PasswordStore(self.state, clock=clock)

    def hubdb(self) -> auth.HubDb:
        db = auth.HubDb.open(self.state)
        self._dbs.append(db)
        self.addCleanup(db.close)
        return db

    def sessions(self, db=None, store=None) -> auth.SessionStore:
        return auth.SessionStore(db or self.hubdb(), store or self.passwords())

    def record(self) -> dict:
        return json.loads(self.auth_path.read_text())

    def sql(self, query: str, args: tuple = ()) -> list[tuple]:
        """Read ``hub.sqlite3`` through a separate stdlib connection (WAL allows it)."""
        connection = sqlite3.connect(f'file:{self.db_path}?mode=ro', uri=True)
        try:
            return connection.execute(query, args).fetchall()
        finally:
            connection.close()

    def tree_bytes(self) -> bytes:
        """Every byte of every file under the state dir (auth.json, db, -wal, -shm, locks, leftovers)."""
        out = b''
        for path in sorted(self.state.rglob('*')):
            if path.is_file() and not path.is_symlink():
                out += path.read_bytes()
        return out

    def assertAbsent(self, blob: bytes, secret: str, where: str) -> None:
        for needle in needles_for(secret):
            self.assertNotIn(needle, blob, f'{secret[:6]}... found in {where} as {needle[:12]!r}')

    def install_auth(self, data: bytes, mode: int = 0o600) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.auth_path)
        descriptor = os.open(self.auth_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, data)
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)


# ===========================================================================================
# password at rest
# ===========================================================================================

class PasswordAtRestTests(AuthCase):
    def test_record_matches_an_independent_scrypt_and_each_write_gets_a_fresh_salt(self):
        store = self.passwords()
        store.set_password(PASSWORD)
        first = self.record()
        self.assertEqual(set(first), {'schema_version', 'kdf', 'n', 'r', 'p', 'dklen', 'salt', 'hash',
                                      'generation', 'changed_at'})
        self.assertEqual([first[k] for k in ('schema_version', 'kdf', 'n', 'r', 'p', 'dklen')],
                         [1, 'scrypt', 32768, 8, 1, 32])
        salt = base64.b64decode(first['salt'], validate=True)
        self.assertEqual(len(salt), 16)
        self.assertEqual(base64.b64decode(first['hash'], validate=True), scrypt_bytes(PASSWORD, salt))
        self.assertEqual(first['changed_at'], T0)
        self.assertNotEqual(base64.b64decode(first['hash']), hashlib.scrypt(PASSWORD.encode(), salt=b'', n=32768, r=8,
                                                                            p=1, dklen=32, maxmem=64 << 20),
                            'the stored hash must not be the unsalted one')

        store.set_password(PASSWORD)  # same password again
        second = self.record()
        self.assertNotEqual(second['salt'], first['salt'], 'a salt is drawn per write')
        self.assertNotEqual(second['hash'], first['hash'], 'same password, different salt, different hash')
        self.assertEqual(base64.b64decode(second['hash']), scrypt_bytes(PASSWORD, base64.b64decode(second['salt'])))

    def test_a_record_written_by_someone_else_with_hashlib_verifies(self):
        salt = bytes(range(16))
        self.state.parent.mkdir(parents=True, mode=0o700)
        self.state.mkdir(mode=0o700)
        record = {'schema_version': 1, 'kdf': 'scrypt', 'n': 32768, 'r': 8, 'p': 1, 'dklen': 32,
                  'salt': base64.b64encode(salt).decode(), 'hash': base64.b64encode(scrypt_bytes(PASSWORD, salt)).decode(),
                  'generation': 41, 'changed_at': 12.5}
        self.install_auth(json.dumps(record).encode())
        store = self.passwords()
        self.assertEqual(store.generation(), 41)
        self.assertEqual(store.verify(PASSWORD), 41)
        self.assertIsNone(store.verify(PASSWORD + 'x'))
        self.assertIsNone(store.verify(PASSWORD[:-1]))
        self.assertIsNone(store.verify(PASSWORD.lower()))
        self.assertIsNone(store.verify(''))
        self.assertIsNone(store.verify('x' * 1025), 'overlong input is refused without hashing')
        self.assertIsNone(store.verify(None))

    def test_the_password_is_in_no_file_no_log_and_no_stream(self):
        records = io.StringIO()
        handler = logging.StreamHandler(records)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            store = self.passwords()
            generation = store.set_password(PASSWORD)
            db = self.hubdb()
            sessions = self.sessions(db, store)
            issued = sessions.create('10.8.0.2', 'agent/1', T0, generation=store.verify(PASSWORD))
            sessions.verify(issued.token, T0 + 1)
            throttle = auth.LoginThrottle(db)
            throttle.record_failure('10.8.0.2')
            log = auth.AuditLog(db)
            log.record('login_failure', 'owner', '10.8.0.2', {'delay_seconds': 0}, now=T0)
            log.record('password_changed', 'owner', '10.8.0.2', {'source': 'web', 'invalidated_sessions': 2}, now=T0)
            with self.assertRaises(auth.PasswordRejected) as short:
                store.set_password('S3cr3t!')
            self.assertEqual(short.exception.reason, 'password_too_short')
            self.assertNotIn('S3cr3t!', repr(short.exception.args) + str(short.exception))
        db.close()  # checkpoint the WAL as well
        streams = (err.getvalue() + out.getvalue() + records.getvalue()).encode()
        for where, blob in (('streams/logs', streams), ('state tree', self.tree_bytes()),
                            ('audit rows', repr(self.sql('SELECT * FROM audit')).encode())):
            self.assertAbsent(blob, PASSWORD, where)
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith('.')], [],
                         'no temp file with a password body is left behind')

    def test_length_rules_count_characters_and_reject_without_touching_the_file(self):
        store = self.passwords()
        for bad in ('', 'a' * 9, '가' * 9, 'a' * 1025, 123456789012, None, b'0123456789abcdef'):
            with self.subTest(bad=repr(bad)[:12]):
                with self.assertRaises(auth.PasswordRejected):
                    store.set_password(bad)
                self.assertFalse(self.auth_path.exists())
        for good in ('a' * 10, '가' * 10, '😀' * 1024, 'a' * 1024, ' ' * 10):
            with self.subTest(good=good[:3] + str(len(good))):
                store.set_password(good)
                self.assertEqual(store.verify(good), store.generation())
        with self.assertRaises(auth.PasswordRejected) as raised:
            store.set_password('a' * 9)
        self.assertEqual(raised.exception.reason, 'password_too_short')
        with self.assertRaises(auth.PasswordRejected) as raised:
            store.set_password('a' * 1025)
        self.assertEqual(raised.exception.reason, 'password_too_long')

    def test_verify_and_compare_use_constant_time_digest_comparison(self):
        store = self.passwords()
        store.set_password(PASSWORD)
        with mock.patch.object(auth.hmac, 'compare_digest', wraps=hmac.compare_digest) as spy:
            self.assertIsNotNone(store.verify(PASSWORD))
            self.assertIsNone(store.verify(OTHER_PASSWORD))
        self.assertEqual(spy.call_count, 2, 'password check goes through hmac.compare_digest')
        for call in spy.call_args_list:
            self.assertEqual({len(arg) for arg in call.args}, {32}, 'digests of equal length are compared')
        source = inspect.getsource(auth.PasswordStore.verify)
        self.assertIn('compare_digest', source)
        self.assertNotRegex(source, r'_scrypt\([^)]*\)\s*==|==\s*record\.digest')


# ===========================================================================================
# modes, umask, symlinks, corruption
# ===========================================================================================

class ModesAndUmaskTests(AuthCase):
    def test_files_are_0600_and_dirs_0700_whatever_the_umask(self):
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(umask)):
                old = os.umask(umask)
                try:
                    root = self.base / f'u{umask:o}' / 'state' / 'omp-workbench' / 'hub'
                    store = auth.PasswordStore(root)
                    store.set_password(PASSWORD)
                    db = auth.HubDb.open(root)
                    try:
                        auth.AuditLog(db).record('logout', 'owner', None, {}, now=T0)
                        for path in sorted(root.rglob('*')):
                            if path.is_dir():
                                self.assertEqual(mode_of(path), 0o700, path)
                            else:
                                self.assertEqual(mode_of(path), 0o600, path)
                        names = {p.name for p in root.iterdir()}
                        self.assertTrue({'auth.json', 'auth.lock', 'hub.sqlite3'} <= names, names)
                        self.assertEqual(mode_of(root), 0o700)
                        self.assertEqual(mode_of(root.parent), 0o700)
                    finally:
                        db.close()
                finally:
                    os.umask(old)

    def test_a_loose_existing_state_dir_is_tightened_not_trusted(self):
        self.state.mkdir(parents=True, mode=0o755)
        os.chmod(self.state, 0o755)
        self.passwords().set_password(PASSWORD)
        self.assertEqual(mode_of(self.state), 0o700)
        os.chmod(self.state, 0o777)
        self.hubdb()
        self.assertEqual(mode_of(self.state), 0o700)

    def test_a_symlinked_state_dir_is_refused_and_nothing_lands_behind_it(self):
        victim = self.base / 'victim'
        victim.mkdir(mode=0o700)
        self.state.parent.mkdir(parents=True, mode=0o700)
        os.symlink(victim, self.state)
        with self.assertRaises(DataDirError):
            self.passwords().set_password(PASSWORD)
        with self.assertRaises(DataDirError):
            auth.HubDb.open(self.state)
        self.assertEqual(list(victim.iterdir()), [])


class AuthInvalidTests(AuthCase):
    def good(self) -> dict:
        salt = bytes(16)
        return {'schema_version': 1, 'kdf': 'scrypt', 'n': 32768, 'r': 8, 'p': 1, 'dklen': 32,
                'salt': base64.b64encode(salt).decode(), 'hash': base64.b64encode(scrypt_bytes(PASSWORD, salt)).decode(),
                'generation': 7, 'changed_at': 1.0}

    def setUp(self):
        super().setUp()
        self.state.parent.mkdir(parents=True, mode=0o700)
        self.state.mkdir(mode=0o700)
        self.store = self.passwords()

    def assertInvalid(self, label: str) -> None:
        with self.assertRaises(auth.AuthInvalid, msg=label) as raised:
            self.store.load()
        self.assertEqual(raised.exception.reason, 'auth_invalid')
        with self.assertRaises(auth.AuthInvalid, msg=label):
            self.store.generation()
        try:
            outcome = self.store.verify(PASSWORD)
        except auth.AuthInvalid:
            outcome = None
        self.assertIsNone(outcome, f'{label}: the right password must not pass on an invalid file')

    def test_a_missing_file_is_the_only_no_password_state(self):
        self.assertIsNone(self.store.load())
        self.assertIsNone(self.store.generation())
        self.assertIsNone(self.store.verify(PASSWORD))
        self.install_auth(json.dumps(self.good()).encode())
        self.assertEqual(self.store.generation(), 7)
        self.assertEqual(self.store.verify(PASSWORD), 7)

    def test_malformed_records_are_auth_invalid_never_no_password(self):
        good = self.good()

        def with_(**changes):
            return {**good, **changes}

        def without(key):
            return {k: v for k, v in good.items() if k != key}

        cases = {
            'empty': b'', 'brace': b'{', 'null': b'null', 'list': b'[]', 'binary': b'\xff\xfe\x00',
            'string': b'"x"', 'trailing junk': json.dumps(good).encode() + b' junk',
            'schema 2': json.dumps(with_(schema_version=2)).encode(),
            'schema true': json.dumps(with_(schema_version=True)).encode(),
            'kdf': json.dumps(with_(kdf='argon2')).encode(),
            'n low': json.dumps(with_(n=16384)).encode(), 'n str': json.dumps(with_(n='32768')).encode(),
            'n float': json.dumps(with_(n=32768.0)).encode(), 'r': json.dumps(with_(r=4)).encode(),
            'p': json.dumps(with_(p=2)).encode(), 'dklen': json.dumps(with_(dklen=64)).encode(),
            'extra key': json.dumps(with_(password='x')).encode(),
            'no hash': json.dumps(without('hash')).encode(), 'no salt': json.dumps(without('salt')).encode(),
            'no generation': json.dumps(without('generation')).encode(),
            'salt chars': json.dumps(with_(salt='!!!!')).encode(),
            'salt 15': json.dumps(with_(salt=base64.b64encode(bytes(15)).decode())).encode(),
            'salt 17': json.dumps(with_(salt=base64.b64encode(bytes(17)).decode())).encode(),
            'hash 31': json.dumps(with_(hash=base64.b64encode(bytes(31)).decode())).encode(),
            'hash null': json.dumps(with_(hash=None)).encode(),
            'gen 0': json.dumps(with_(generation=0)).encode(), 'gen -1': json.dumps(with_(generation=-1)).encode(),
            'gen str': json.dumps(with_(generation='7')).encode(), 'gen bool': json.dumps(with_(generation=True)).encode(),
            'gen float': json.dumps(with_(generation=7.0)).encode(),
            'changed_at str': json.dumps(with_(changed_at='now')).encode(),
            'oversize': json.dumps(with_(padding='x' * 70_000)).encode(),
        }
        for label, data in cases.items():
            with self.subTest(label):
                self.install_auth(data)
                self.assertInvalid(label)

    def test_loose_modes_are_auth_invalid(self):
        data = json.dumps(self.good()).encode()
        for mode in (0o644, 0o640, 0o604, 0o660, 0o666, 0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                self.install_auth(data, mode)
                self.assertInvalid(oct(mode))
        self.install_auth(data, 0o600)
        self.assertEqual(self.store.generation(), 7, 'the same bytes at 0600 are fine')

    def test_symlinks_and_special_files_are_auth_invalid_even_when_the_target_is_perfect(self):
        data = json.dumps(self.good()).encode()
        target = self.state / 'real.json'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, data)
        os.close(fd)
        os.symlink(target, self.auth_path)
        self.assertInvalid('symlink to a valid 0600 file')
        os.unlink(self.auth_path)
        os.symlink(self.state / 'does-not-exist', self.auth_path)
        self.assertInvalid('dangling symlink (not "absent")')
        os.unlink(self.auth_path)
        os.mkdir(self.auth_path, 0o700)
        self.assertInvalid('directory')
        os.rmdir(self.auth_path)
        os.mkfifo(self.auth_path, 0o600)
        self.assertInvalid('fifo')

    def test_an_invalid_file_never_authorizes_an_existing_session(self):
        store, db = self.store, self.hubdb()
        generation = store.set_password(PASSWORD)
        sessions = self.sessions(db, store)
        issued = sessions.create('a', 'ua', T0, generation=generation)
        self.assertIsNotNone(sessions.verify(issued.token, T0 + 1))
        good_bytes = self.auth_path.read_bytes()
        for label, install in (('0644', lambda: self.install_auth(good_bytes, 0o644)),
                               ('garbage', lambda: self.install_auth(b'{')),
                               ('symlink', lambda: (os.unlink(self.auth_path), os.symlink(self.base, self.auth_path)))):
            with self.subTest(label):
                install()
                self.assertIsNone(sessions.verify(issued.token, T0 + 1))
                self.assertIsNone(sessions.reissue(issued.token, T0 + 1))
        self.install_auth(good_bytes)
        self.assertIsNotNone(sessions.verify(issued.token, T0 + 1), 'the session returns with the valid file')
        self.assertEqual(self.sql('SELECT count(*) FROM sessions'), [(1,)], 'a bad file did not delete rows')

    def test_set_password_repairs_a_bad_file_without_following_or_resurrecting(self):
        store, db = self.store, self.hubdb()
        generation = store.set_password(PASSWORD)
        sessions = self.sessions(db, store)
        old = sessions.create('a', 'ua', T0, generation=generation)
        victim = self.base / 'victim.json'
        victim.write_text('victim-body')
        os.chmod(victim, 0o600)
        os.unlink(self.auth_path)
        os.symlink(victim, self.auth_path)
        store.set_password(OTHER_PASSWORD)
        self.assertEqual(victim.read_text(), 'victim-body', 'the write replaced the link, not its target')
        self.assertFalse(self.auth_path.is_symlink())
        self.assertEqual(mode_of(self.auth_path), 0o600)
        self.assertIsNotNone(store.verify(OTHER_PASSWORD))
        self.assertIsNone(sessions.verify(old.token, T0 + 1), 'a repaired file does not revive older sessions')

        self.install_auth(self.auth_path.read_bytes(), 0o644)  # loose mode again
        store.set_password(PASSWORD)
        self.assertEqual(mode_of(self.auth_path), 0o600)

    def test_a_symlinked_lock_file_is_refused_and_writes_nothing(self):
        victim = self.base / 'lockvictim'
        victim.write_text('keep')
        os.symlink(victim, self.state / 'auth.lock')
        with self.assertRaises(OSError):
            self.store.set_password(PASSWORD)
        self.assertEqual(victim.read_text(), 'keep')
        self.assertFalse(self.auth_path.exists())


# ===========================================================================================
# generation, sessions, CSRF
# ===========================================================================================

class GenerationTests(AuthCase):
    def test_every_write_bumps_the_generation_and_kills_every_older_session(self):
        store, db = self.passwords(), self.hubdb()
        sessions = self.sessions(db, store)
        g1 = store.set_password(PASSWORD)
        old = [sessions.create('10.0.0.%d' % i, 'ua', T0, generation=g1) for i in range(4)]
        self.assertTrue(all(sessions.verify(s.token, T0 + 5) for s in old))
        g2 = store.set_password(OTHER_PASSWORD)
        self.assertEqual(g2, g1 + 1)
        self.assertEqual(self.record()['generation'], g2)
        for session in old:
            self.assertIsNone(sessions.verify(session.token, T0 + 5), 'older generation')
        self.assertIsNone(store.verify(PASSWORD), 'the old password stopped working')
        self.assertEqual(store.verify(OTHER_PASSWORD), g2)
        new = sessions.create('a', 'ua', T0, generation=store.verify(OTHER_PASSWORD))
        self.assertIsNotNone(sessions.verify(new.token, T0 + 5))
        g3 = store.set_password(OTHER_PASSWORD)  # same password again still bumps
        self.assertEqual(g3, g2 + 1)
        self.assertIsNone(sessions.verify(new.token, T0 + 5))

    def test_a_session_created_with_a_stale_generation_is_never_valid(self):
        store, db = self.passwords(), self.hubdb()
        sessions = self.sessions(db, store)
        g1 = store.set_password(PASSWORD)
        g2 = store.set_password(PASSWORD)
        stale = sessions.create('a', 'ua', T0, generation=g1)
        self.assertIsNone(sessions.verify(stale.token, T0 + 1))
        future = sessions.create('a', 'ua', T0, generation=g2 + 1)
        self.assertIsNone(sessions.verify(future.token, T0 + 1))

    def test_sessions_survive_a_hub_restart_but_not_a_generation_change(self):
        store = self.passwords()
        generation = store.set_password(PASSWORD)
        db = auth.HubDb.open(self.state)
        issued = auth.SessionStore(db, store).create('a', 'ua', T0, generation=generation)
        db.close()
        reopened = self.hubdb()
        sessions = auth.SessionStore(reopened, store)
        self.assertIsNotNone(sessions.verify(issued.token, T0 + 3600))
        store.set_password(PASSWORD)
        self.assertIsNone(sessions.verify(issued.token, T0 + 3600))

    def test_reissue_after_a_password_change_keeps_only_the_current_login(self):
        store, db = self.passwords(), self.hubdb()
        sessions = self.sessions(db, store)
        g1 = store.set_password(PASSWORD)
        current = sessions.create('a', 'ua-current', T0, generation=g1)
        others = [sessions.create('b', 'ua', T0 + 10, generation=g1) for _ in range(3)]
        g2 = store.set_password(OTHER_PASSWORD)
        self.assertIsNone(sessions.verify(current.token, T0 + 20), 'rejected until reissued')
        result = sessions.reissue(current.token, T0 + 20)
        self.assertEqual(result.invalidated, 3)
        fresh = result.session
        self.assertNotEqual(fresh.token, current.token)
        self.assertNotEqual(fresh.csrf, current.csrf, 'a reissued login gets a new CSRF token')
        self.assertEqual(fresh.expires_at, current.expires_at, 'reissue does not extend the 24 h')
        verified = sessions.verify(fresh.token, T0 + 20)
        self.assertEqual((verified.password_generation, verified.user_agent), (g2, 'ua-current'))
        self.assertIsNone(sessions.verify(current.token, T0 + 20), 'the old token is gone')
        for other in others:
            self.assertIsNone(sessions.verify(other.token, T0 + 20))
        self.assertEqual(self.sql('SELECT count(*) FROM sessions'), [(1,)])
        self.assertIsNone(sessions.verify(fresh.token, current.expires_at), 'still absolute 24 h')
        self.assertIsNone(sessions.reissue(current.token, T0 + 30), 'a spent token cannot be reissued twice')
        self.assertIsNone(sessions.reissue('never-issued', T0 + 30))

    def test_reissue_refuses_an_expired_session_and_leaves_the_table_alone(self):
        store, db = self.passwords(), self.hubdb()
        sessions = self.sessions(db, store)
        g1 = store.set_password(PASSWORD)
        old = sessions.create('a', 'ua', T0, generation=g1)
        other = sessions.create('b', 'ua', T0, generation=g1)
        store.set_password(OTHER_PASSWORD)
        self.assertIsNone(sessions.reissue(old.token, T0 + DAY))
        self.assertEqual(self.sql('SELECT count(*) FROM sessions'), [(2,)], 'nothing was deleted by the refusal')
        self.assertIsNotNone(other)

    def test_delete_and_delete_all_except(self):
        store, db = self.passwords(), self.hubdb()
        sessions = self.sessions(db, store)
        generation = store.set_password(PASSWORD)
        issued = [sessions.create('a', 'ua', T0, generation=generation) for _ in range(5)]
        self.assertEqual(sessions.delete_all_except(issued[2].token), 4)
        self.assertIsNotNone(sessions.verify(issued[2].token, T0 + 1))
        self.assertEqual(self.sql('SELECT count(*) FROM sessions'), [(1,)])
        self.assertEqual(sessions.delete_all_except('not-a-token'), 1, 'unknown token to keep means delete all')
        issued = sessions.create('a', 'ua', T0, generation=generation)
        sessions.delete(issued.token)
        self.assertIsNone(sessions.verify(issued.token, T0 + 1))
        sessions.delete(None)
        sessions.delete('x' * 10_000)

    def test_a_password_change_racing_session_checks_never_lets_the_old_generation_through(self):
        store, db = self.passwords(), self.hubdb()
        sessions = self.sessions(db, store)
        generation = store.set_password(PASSWORD)
        for round_ in range(6):
            issued = sessions.create('a', 'ua', T0, generation=generation)
            self.assertIsNotNone(sessions.verify(issued.token, T0 + 1))
            stop, leaks, errors = threading.Event(), [], []

            def hammer():
                while not stop.is_set():
                    try:
                        sessions.verify(issued.token, T0 + 1)
                    except BaseException as exc:  # a half-written file must never surface
                        errors.append(repr(exc))
            threads = [threading.Thread(target=hammer) for _ in range(3)]
            for thread in threads:
                thread.start()
            try:
                generation = store.set_password(OTHER_PASSWORD if round_ % 2 else PASSWORD)
                # set_password has returned: no later verification may accept the old session.
                for _ in range(50):
                    if sessions.verify(issued.token, T0 + 1) is not None:
                        leaks.append(round_)
            finally:
                stop.set()
                for thread in threads:
                    thread.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(leaks, [], 'old-generation session accepted after the password write finished')

    def test_concurrent_writers_lose_no_generation_and_leave_a_consistent_record(self):
        store = self.passwords()
        start = store.set_password(PASSWORD)
        passwords = [[f'writer-{w}-password-{i:02d}' for i in range(4)] for w in range(2)]
        errors = []

        def write(batch):
            try:
                for text in batch:
                    store.set_password(text)
            except BaseException as exc:
                errors.append(repr(exc))
        threads = [threading.Thread(target=write, args=(batch,)) for batch in passwords]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        record = self.record()
        self.assertEqual(record['generation'], start + 8, 'a lost update would leave a smaller number')
        salt = base64.b64decode(record['salt'])
        matches = [t for batch in passwords for t in batch if scrypt_bytes(t, salt) == base64.b64decode(record['hash'])]
        self.assertEqual(len(matches), 1, 'salt and hash belong to one write (no torn record)')

    def test_readers_never_see_a_torn_file_while_it_is_rewritten(self):
        store = self.passwords()
        store.set_password(PASSWORD)
        stop, seen, errors = threading.Event(), [], []

        def read():
            while not stop.is_set():
                try:
                    seen.append(store.load().generation)
                except BaseException as exc:
                    errors.append(repr(exc))
        thread = threading.Thread(target=read)
        thread.start()
        try:
            for i in range(8):
                store.set_password(f'rewrite-password-{i:03d}')
        finally:
            stop.set()
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertTrue(seen)
        self.assertEqual(seen, sorted(seen), 'generations only move forward')

    def test_verify_never_pairs_the_old_password_with_a_newer_generation(self):
        store = self.passwords()
        first = store.set_password(PASSWORD)
        stop, results = threading.Event(), []

        def read():
            while not stop.is_set():
                results.append(store.verify(PASSWORD))
        thread = threading.Thread(target=read)
        thread.start()
        try:
            for _ in range(3):
                store.set_password(OTHER_PASSWORD)
        finally:
            stop.set()
            thread.join(30)
        self.assertTrue(results)
        self.assertEqual({r for r in results if r is not None}, {first}, 'the old password only ever matches its own generation')


class SessionTests(AuthCase):
    def make(self):
        store, db = self.passwords(), self.hubdb()
        generation = store.set_password(PASSWORD)
        return store, db, auth.SessionStore(db, store), generation

    def test_a_session_expires_at_exactly_login_plus_24_hours_with_no_extension(self):
        _, _, sessions, generation = self.make()
        issued = sessions.create('10.8.0.2', 'ua', T0, generation=generation)
        self.assertEqual(issued.expires_at, T0 + DAY)
        self.assertEqual(self.sql('SELECT created_at, expires_at FROM sessions'), [(T0, T0 + DAY)])
        for when in (T0, T0 + 1, T0 + 3600, T0 + DAY - 3600, T0 + DAY - 0.001):
            self.assertIsNotNone(sessions.verify(issued.token, when), when)
        self.assertIsNone(sessions.verify(issued.token, T0 + DAY), 'refused at exactly 24 h')
        self.assertIsNone(sessions.verify(issued.token, T0 + DAY + 0.001))
        self.assertIsNone(sessions.verify(issued.token, T0 + 10 * DAY))
        self.assertEqual(self.sql('SELECT expires_at FROM sessions'), [(T0 + DAY,)], 'verifying never extends it')

    def test_two_logins_expire_independently_from_their_own_start(self):
        _, _, sessions, generation = self.make()
        early = sessions.create('a', 'ua', T0, generation=generation)
        late = sessions.create('a', 'ua', T0 + 7200, generation=generation)
        when = T0 + DAY + 1
        self.assertIsNone(sessions.verify(early.token, when))
        self.assertIsNotNone(sessions.verify(late.token, when))

    def test_tokens_are_32_random_bytes_and_only_their_sha256_is_stored(self):
        store, db, sessions, generation = self.make()
        with mock.patch.object(auth.secrets, 'token_bytes', wraps=__import__('secrets').token_bytes) as spy:
            issued = sessions.create('10.8.0.2', 'ua', T0, generation=generation)
        self.assertEqual([call.args for call in spy.call_args_list], [(32,), (32,)], 'token and csrf: 32 CSPRNG bytes each')
        for value in (issued.token, issued.csrf):
            self.assertRegex(value, r'^[A-Za-z0-9_-]{43}$')
            self.assertEqual(len(base64.urlsafe_b64decode(value + '=')), 32)
        self.assertNotEqual(issued.token, issued.csrf)
        batch = [sessions.create('a', 'ua', T0, generation=generation) for _ in range(300)]
        self.assertEqual(len({s.token for s in batch} | {s.csrf for s in batch}), 600, 'no repeats')
        chars = set(''.join(s.token for s in batch))
        self.assertGreater(len(chars), 55, 'tokens use the whole base64url alphabet')
        db.close()
        rows = self.sql('SELECT token_hash, csrf_token, client_addr, user_agent FROM sessions WHERE client_addr = ?',
                        ('10.8.0.2',))
        self.assertEqual(rows, [(hashlib.sha256(issued.token.encode()).hexdigest(), issued.csrf, '10.8.0.2', 'ua')])
        blob = self.tree_bytes()
        for session in batch + [issued]:
            self.assertNotIn(session.token.encode(), blob, 'raw session token reached a file')

    def test_a_leaked_db_row_cannot_be_replayed_as_a_cookie(self):
        _, db, sessions, generation = self.make()
        issued = sessions.create('a', 'ua', T0, generation=generation)
        (token_hash, csrf), = self.sql('SELECT token_hash, csrf_token FROM sessions')
        self.assertIsNone(sessions.verify(token_hash, T0 + 1), 'the stored hash is not a credential')
        self.assertIsNone(sessions.verify(csrf, T0 + 1), 'the csrf token is not a session token')
        self.assertIsNotNone(sessions.verify(issued.token, T0 + 1))

    def test_the_csrf_token_belongs_to_exactly_one_session(self):
        _, _, sessions, generation = self.make()
        a = sessions.create('a', 'ua', T0, generation=generation)
        b = sessions.create('b', 'ub', T0, generation=generation)
        self.assertEqual(sessions.verify(a.token, T0 + 1).csrf, a.csrf)
        self.assertEqual(sessions.verify(b.token, T0 + 1).csrf, b.csrf)
        self.assertNotEqual(a.csrf, b.csrf)
        self.assertNotEqual(sessions.verify(a.token, T0 + 1).csrf, b.csrf)

    def test_junk_tokens_are_refused_without_error(self):
        _, _, sessions, _ = self.make()
        for junk in (None, '', b'bytes', 0, "' OR '1'='1", '%', 'a' * 257, 'a' * 100_000, '\x00', '😀', ['x'], {}):
            with self.subTest(junk=repr(junk)[:20]):
                self.assertIsNone(sessions.verify(junk, T0))
                sessions.delete(junk)
        self.assertIsNone(sessions.reissue(None, T0))


# ===========================================================================================
# login throttle
# ===========================================================================================

class ThrottleTests(AuthCase):
    def throttle(self) -> auth.LoginThrottle:
        return auth.LoginThrottle(self.hubdb())

    def test_delay_grows_per_spec_f_of_n(self):
        throttle = self.throttle()
        seen = []
        for failures in range(0, 14):
            seen.append(throttle.delay_for('10.8.0.2'))
            self.assertEqual(seen[-1], f_of(failures), f'after {failures} failures')
            throttle.record_failure('10.8.0.2')
        self.assertEqual(seen[:8], [0, 0, 0, 0, 1, 2, 4, 8])
        self.assertEqual(seen[-1], 30, 'capped at 30 s')
        self.assertEqual(throttle.delay_for('10.8.0.2'), throttle.delay_for('10.8.0.2'), 'reading does not count')

    def test_per_address_counts_are_independent_but_a_global_pressure_floor_exists(self):
        throttle = self.throttle()
        for _ in range(9):
            throttle.record_failure('attacker')  # global = 9 -> f(3) = 0
        self.assertEqual(throttle.delay_for('attacker'), f_of(9))
        self.assertEqual(throttle.delay_for('someone-else'), 0, 'another address starts clean while global/3 <= 3')
        for _ in range(3):
            throttle.record_failure('attacker')  # global = 12 -> f(4) = 1
        self.assertEqual(throttle.delay_for('someone-else'), f_of(12 // 3))
        self.assertEqual(throttle.delay_for('someone-else'), 1)
        for _ in range(18):
            throttle.record_failure('other-%d' % _)  # global = 30 -> f(10) = 30
        self.assertEqual(throttle.delay_for('brand-new'), 30)
        self.assertEqual(throttle.delay_for('other-3'), 30)

    def test_distributed_guessing_from_many_addresses_is_still_slowed(self):
        throttle = self.throttle()
        for i in range(24):  # each address fails once; global = 24 -> f(8) = 16
            throttle.record_failure(f'10.0.0.{i}')
        self.assertEqual(throttle.delay_for('10.0.0.200'), f_of(24 // 3))
        self.assertEqual(throttle.delay_for('10.0.0.200'), 16)

    def test_a_success_resets_that_address_and_the_global_count_only(self):
        throttle = self.throttle()
        for _ in range(7):
            throttle.record_failure('a')
        for _ in range(7):
            throttle.record_failure('b')
        self.assertEqual((throttle.delay_for('a'), throttle.delay_for('b')), (8, 8))
        throttle.record_success('a')
        self.assertEqual(throttle.delay_for('a'), 0)
        self.assertEqual(throttle.delay_for('b'), 8, "b's own failures stay")
        self.assertEqual(throttle.delay_for('c'), 0, 'global dropped to 0')
        throttle.record_success('never-failed')
        self.assertEqual(throttle.delay_for('b'), 8)

    def test_counts_survive_a_hub_restart_and_none_address_is_tolerated(self):
        db = auth.HubDb.open(self.state)
        throttle = auth.LoginThrottle(db)
        for _ in range(6):
            throttle.record_failure('10.8.0.2')
        throttle.record_failure(None)
        db.close()
        again = self.throttle()
        self.assertEqual(again.delay_for('10.8.0.2'), f_of(6))
        self.assertEqual(again.delay_for(None), 0)

    def test_the_delay_function_is_exactly_the_spec_table(self):
        expected = {0: 0, 1: 0, 2: 0, 3: 0, 4: 1, 5: 2, 6: 4, 7: 8, 8: 16, 9: 30, 10: 30, 50: 30, 10_000: 30}
        for failures, seconds in expected.items():
            self.assertEqual(auth.delay_seconds(failures), seconds, failures)


# ===========================================================================================
# API keys
# ===========================================================================================

KEY_RE = re.compile(r'^wbk_[a-z2-7]{10}_[A-Za-z0-9_-]{43}$')


class ApiKeyTests(AuthCase):
    def keys(self) -> auth.ApiKeyStore:
        return auth.ApiKeyStore(self.hubdb())

    def test_format_uniqueness_and_hash_only_storage(self):
        keys = self.keys()
        issued = [keys.issue(f'k{i}', 'requester' if i % 2 else 'editor', {'all': True}, None, now=T0) for i in range(60)]
        raws = [k.raw for k in issued]
        for raw in raws:
            self.assertRegex(raw, KEY_RE)
        self.assertEqual(len(set(raws)), 60)
        self.assertEqual(len({k.key_id for k in issued}), 60)
        self.assertEqual(len({raw[15:] for raw in raws}), 60)
        self.assertEqual(len(base64.urlsafe_b64decode(raws[0][15:] + '=')), 32, '256-bit secret')
        for k in issued:
            self.assertEqual(k.raw[4:14], k.key_id)
        self.hubdb().close()
        rows = {r[0]: r for r in self.sql('SELECT key_id, secret_hash, name FROM api_keys')}
        for k in issued:
            self.assertEqual(rows[k.key_id][1], hashlib.sha256(k.raw[15:].encode()).hexdigest())
        blob = self.tree_bytes()
        for k in issued:
            self.assertNotIn(k.raw.encode(), blob)
            self.assertNotIn(k.raw[15:].encode(), blob, 'the secret itself is nowhere on disk')

    def test_listing_exposes_metadata_but_never_a_secret_or_its_hash(self):
        keys = self.keys()
        one = keys.issue('Claude agent', 'editor', {'projects': ['p1', 'p2']}, 30, now=T0)
        keys.verify(one.raw, T0 + 5)
        keys.flush()
        (info,) = keys.list()
        self.assertEqual((info.key_id, info.name, info.grade, info.scope, info.created_at, info.expires_at,
                          info.revoked_at, info.last_used_at, info.system),
                         (one.key_id, 'Claude agent', 'editor', {'projects': ['p1', 'p2']}, T0, T0 + 30 * DAY,
                          None, T0 + 5, False))
        secret_hash = hashlib.sha256(one.raw[15:].encode()).hexdigest()
        text = repr(keys.list()) + ' '.join(f.name for f in dataclasses.fields(auth.ApiKeyInfo))
        self.assertNotIn(one.raw[15:], text)
        self.assertNotIn(secret_hash, text)
        self.assertFalse(any('hash' in f.name or 'secret' in f.name for f in dataclasses.fields(auth.ApiKeyInfo)))
        principal = keys.verify(one.raw, T0 + 6)
        self.assertNotIn(one.raw[15:], repr(principal))

    def test_issue_refuses_bad_input_and_stores_nothing(self):
        keys = self.keys()
        bad = [
            dict(name='', grade='editor', scope={'all': True}, days=None, why='name_invalid'),
            dict(name='x' * 65, grade='editor', scope={'all': True}, days=None, why='name_invalid'),
            dict(name='a/b', grade='editor', scope={'all': True}, days=None, why='name_invalid'),
            dict(name='line\nbreak', grade='editor', scope={'all': True}, days=None, why='name_invalid'),
            dict(name='한글', grade='editor', scope={'all': True}, days=None, why='name_invalid'),
            dict(name=None, grade='editor', scope={'all': True}, days=None, why='name_invalid'),
            dict(name='board-agent', grade='editor', scope={'all': True}, days=None, why='name_reserved'),
            dict(name='ok', grade='owner', scope={'all': True}, days=None, why='grade_invalid'),
            dict(name='ok', grade='', scope={'all': True}, days=None, why='grade_invalid'),
            dict(name='ok', grade='editor', scope={}, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope={'all': False}, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope={'all': 1}, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope={'projects': []}, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope={'projects': 'p1'}, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope={'projects': [1]}, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope={'projects': ['a', 'a']}, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope={'all': True, 'projects': ['a']}, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope=None, days=None, why='scope_invalid'),
            dict(name='ok', grade='editor', scope={'all': True}, days=0, why='expiry_invalid'),
            dict(name='ok', grade='editor', scope={'all': True}, days=-1, why='expiry_invalid'),
            dict(name='ok', grade='editor', scope={'all': True}, days=3651, why='expiry_invalid'),
            dict(name='ok', grade='editor', scope={'all': True}, days=True, why='expiry_invalid'),
            dict(name='ok', grade='editor', scope={'all': True}, days=1.5, why='expiry_invalid'),
            dict(name='ok', grade='editor', scope={'all': True}, days='7', why='expiry_invalid'),
        ]
        for case in bad:
            with self.subTest(**{k: v for k, v in case.items() if k != 'scope'}):
                with self.assertRaises(auth.ApiKeyRejected) as raised:
                    keys.issue(case['name'], case['grade'], case['scope'], case['days'], now=T0)
                self.assertEqual(raised.exception.reason, case['why'])
        self.assertEqual(keys.list(), [])
        for name in ('a', 'x' * 64, 'My key-1.2_3'):
            keys.issue(name, 'requester', {'projects': ['p']}, 1, now=T0)
        keys.issue('last', 'requester', {'all': True}, 3650, now=T0)
        with self.assertRaises(auth.ApiKeyRejected) as taken:
            keys.issue('a', 'editor', {'all': True}, None, now=T0)
        self.assertEqual(taken.exception.reason, 'name_taken')

    def test_verify_refuses_each_bad_key_with_its_own_reason(self):
        keys = self.keys()
        live = keys.issue('live', 'editor', {'all': True}, None, now=T0)
        dead = keys.issue('dead', 'editor', {'all': True}, None, now=T0)
        short = keys.issue('short', 'requester', {'projects': ['p']}, 1, now=T0)
        keys.revoke(dead.key_id, now=T0 + 5)
        principal = keys.verify(live.raw, T0 + 10)
        self.assertEqual((principal.name, principal.grade, principal.scope, principal.system, principal.key_id),
                         ('live', 'editor', {'all': True}, False, live.key_id))

        def refusal(raw, now=T0 + 10):
            with self.assertRaises(auth.ApiKeyRefused) as raised:
                keys.verify(raw, now)
            return raised.exception.reason

        self.assertEqual(refusal(dead.raw), 'revoked')
        self.assertEqual(refusal(short.raw, T0 + DAY + 1), 'expired')
        self.assertEqual(refusal(short.raw, T0 + 30 * DAY), 'expired')
        self.assertEqual(keys.verify(short.raw, T0 + DAY - 1).name, 'short')
        wrong_secret = live.raw[:15] + ('A' if live.raw[15] != 'A' else 'B') + live.raw[16:]
        self.assertEqual(refusal(wrong_secret), 'unknown')
        flipped_last = live.raw[:-1] + ('A' if live.raw[-1] != 'A' else 'Q')
        self.assertIn(refusal(flipped_last), ('unknown', 'malformed'))
        swapped = live.raw[:15] + dead.raw[15:]
        self.assertEqual(refusal(swapped), 'unknown', "key A's id with key B's secret")
        unknown = 'wbk_' + ('a' * 10) + live.raw[14:]
        self.assertEqual(refusal(unknown), 'unknown')
        wrong_revoked = dead.raw[:15] + ('A' if dead.raw[15] != 'A' else 'B') + dead.raw[16:]
        self.assertEqual(refusal(wrong_revoked), 'unknown', 'revoked state is not revealed without the secret')
        for junk in (None, '', b'wbk', 7, live.raw + 'x', live.raw[:-1], ' ' + live.raw, live.raw + '\n',
                     live.raw.upper(), 'WBK' + live.raw[3:], 'sk_' + live.raw[4:], live.raw.replace('_', '-', 1),
                     live.raw[:14] + '-' + live.raw[15:], live.raw[:4] + live.raw[4:14].upper() + live.raw[14:],
                     live.raw[:-1] + '=', live.raw[:-1] + 'é', 'wbk_' + '1' * 10 + live.raw[14:],
                     live.raw[:4] + '0' * 10 + live.raw[14:], "wbk_' OR 1=1 --" + live.raw[15:]):
            with self.subTest(junk=repr(junk)[:24]):
                self.assertEqual(refusal(junk), 'malformed')
        self.assertEqual(refusal(live.raw[:-1] + ('A' if live.raw[-1] != 'A' else 'B')) in ('unknown', 'malformed'), True)

    def test_a_non_canonical_secret_encoding_is_not_accepted_as_an_alias(self):
        keys = self.keys()
        issued = keys.issue('canon', 'editor', {'all': True}, None, now=T0)
        secret = issued.raw[15:]
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
        # the 43rd char carries only 4 significant bits; any sibling with equal bits decodes to the same bytes
        for last in alphabet:
            if last == secret[-1]:
                continue
            raw = issued.raw[:-1] + last
            if base64.urlsafe_b64decode(raw[15:] + '=') == base64.urlsafe_b64decode(secret + '='):
                with self.assertRaises(auth.ApiKeyRefused):
                    keys.verify(raw, T0 + 1)

    def test_verification_compares_digests_in_constant_time_even_for_unknown_ids(self):
        keys = self.keys()
        issued = keys.issue('ct', 'editor', {'all': True}, None, now=T0)
        wrong = issued.raw[:15] + ('A' if issued.raw[15] != 'A' else 'B') + issued.raw[16:]
        unknown = 'wbk_' + 'a' * 10 + issued.raw[14:]
        with mock.patch.object(auth.hmac, 'compare_digest', wraps=hmac.compare_digest) as spy:
            keys.verify(issued.raw, T0)
            with self.assertRaises(auth.ApiKeyRefused):
                keys.verify(wrong, T0)
            with self.assertRaises(auth.ApiKeyRefused):
                keys.verify(unknown, T0)
        self.assertEqual(spy.call_count, 3, 'one digest comparison per attempt: no early exit on a missing/known id')
        for call in spy.call_args_list:
            self.assertEqual({len(arg) for arg in call.args}, {64})
        source = inspect.getsource(auth.ApiKeyStore.verify)
        self.assertIn('compare_digest', source)
        self.assertNotRegex(source, r'_secret_hash\([^)]*\)\s*==|==\s*stored|secret_hash\s*==')

    def test_last_used_is_coalesced_in_memory_and_written_only_on_flush(self):
        keys = self.keys()
        one = keys.issue('one', 'editor', {'all': True}, None, now=T0)
        two = keys.issue('two', 'editor', {'all': True}, None, now=T0)
        for when in (T0 + 3, T0 + 9, T0 + 5):  # out of order on purpose
            keys.verify(one.raw, when)
        keys.verify(two.raw, T0 + 4)
        self.assertEqual(self.sql('SELECT key_id, last_used_at FROM api_keys ORDER BY name'),
                         [(one.key_id, None), (two.key_id, None)], 'verify itself writes nothing')
        self.assertEqual([k.last_used_at for k in keys.list()], [None, None])
        self.assertEqual(keys.flush(), 2)
        self.assertEqual(dict(self.sql('SELECT key_id, last_used_at FROM api_keys')), {one.key_id: T0 + 9, two.key_id: T0 + 4})
        self.assertEqual(keys.flush(), 0, 'nothing pending the second time')
        keys.verify(one.raw, T0 + 2)  # an earlier clock reading never moves the stored time back
        keys.flush()
        self.assertEqual(dict(self.sql('SELECT key_id, last_used_at FROM api_keys'))[one.key_id], T0 + 9)
        keys.verify(one.raw, T0 + 20)
        keys.flush()
        self.assertEqual(dict(self.sql('SELECT key_id, last_used_at FROM api_keys'))[one.key_id], T0 + 20)

    def test_refused_calls_do_not_count_as_use(self):
        keys = self.keys()
        live = keys.issue('live', 'editor', {'all': True}, None, now=T0)
        revoked = keys.issue('revoked', 'editor', {'all': True}, None, now=T0)
        expiring = keys.issue('expiring', 'editor', {'all': True}, 1, now=T0)
        keys.revoke(revoked.key_id, now=T0 + 1)
        for raw, now in ((revoked.raw, T0 + 2), (expiring.raw, T0 + DAY + 1), (live.raw[:15] + ('A' if live.raw[15] != 'A' else 'B') + live.raw[16:], T0 + 2),
                         ('junk', T0)):
            with self.assertRaises(auth.ApiKeyRefused):
                keys.verify(raw, now)
        self.assertEqual(keys.flush(), 0)
        self.assertEqual({k.last_used_at for k in keys.list()}, {None})

    def test_a_failed_flush_keeps_the_pending_times_for_the_next_one(self):
        db = self.hubdb()
        keys = auth.ApiKeyStore(db)
        issued = keys.issue('k', 'editor', {'all': True}, None, now=T0)
        keys.verify(issued.raw, T0 + 7)
        with mock.patch.object(db, 'write', side_effect=sqlite3.OperationalError('disk I/O error')):
            with self.assertRaises(sqlite3.OperationalError):
                keys.flush()
        self.assertEqual(self.sql('SELECT last_used_at FROM api_keys'), [(None,)])
        self.assertEqual(keys.flush(), 1)
        self.assertEqual(self.sql('SELECT last_used_at FROM api_keys'), [(T0 + 7,)])

    def test_a_thousand_consecutive_verifications_are_never_rate_limited(self):
        keys = self.keys()
        issued = keys.issue('busy', 'requester', {'projects': ['p']}, None, now=T0)
        for i in range(1000):
            keys.verify(issued.raw, T0 + i * 0.001)
        self.assertEqual(keys.flush(), 1)
        self.assertEqual(self.sql('SELECT count(*) FROM api_keys'), [(1,)])

    def test_revocation_applies_to_the_next_call_and_is_visible_in_the_list(self):
        keys = self.keys()
        issued = keys.issue('r', 'requester', {'projects': ['p']}, None, now=T0)
        self.assertEqual(keys.verify(issued.raw, T0 + 1).name, 'r')
        info = keys.revoke(issued.key_id, now=T0 + 2)
        self.assertEqual(info.revoked_at, T0 + 2)
        with self.assertRaises(auth.ApiKeyRefused) as raised:
            keys.verify(issued.raw, T0 + 3)
        self.assertEqual(raised.exception.reason, 'revoked')
        self.assertEqual(keys.list()[0].revoked_at, T0 + 2)
        for key_id, why in (('nope', 'not_found'), (issued.key_id, 'already_revoked')):
            with self.assertRaises(auth.ApiKeyRejected) as bad:
                keys.revoke(key_id, now=T0 + 4)
            self.assertEqual(bad.exception.reason, why)
        self.assertEqual(keys.list()[0].revoked_at, T0 + 2, 'a second revoke did not rewrite the time')


class SystemKeyTests(AuthCase):
    def test_board_agent_key_rotation_invalidates_the_previous_key(self):
        db = self.hubdb()
        keys = auth.ApiKeyStore(db)
        owner = keys.issue('owner key', 'editor', {'all': True}, None, now=T0)
        first = keys.rotate_system_key(now=T0 + 1)
        principal = keys.verify(first.raw, T0 + 2)
        self.assertEqual((principal.name, principal.grade, principal.scope, principal.system),
                         ('board-agent', 'editor', {'all': True}, True))
        second = keys.rotate_system_key(now=T0 + 3)
        self.assertNotEqual(first.raw, second.raw)
        with self.assertRaises(auth.ApiKeyRefused) as old:
            keys.verify(first.raw, T0 + 4)
        self.assertIn(old.exception.reason, ('unknown', 'revoked'))
        self.assertTrue(keys.verify(second.raw, T0 + 4).system)
        self.assertEqual(keys.verify(owner.raw, T0 + 4).name, 'owner key', 'owner keys are untouched by rotation')
        self.assertEqual(self.sql('SELECT count(*) FROM api_keys WHERE system = 1'), [(1,)])
        self.assertEqual(self.sql('SELECT name, expires_at, revoked_at FROM api_keys WHERE system = 1'),
                         [('board-agent', None, None)])
        self.assertEqual([k.name for k in keys.list()][0], 'board-agent', 'system key listed first')
        self.assertNotIn(first.raw.encode(), self.tree_bytes())
        self.assertNotIn(second.raw[15:].encode(), self.tree_bytes())

    def test_the_system_key_is_not_a_revocable_list_entry_and_the_name_is_reserved(self):
        keys = auth.ApiKeyStore(self.hubdb())
        system = keys.rotate_system_key(now=T0)
        with self.assertRaises(auth.ApiKeyRejected) as raised:
            keys.revoke(system.key_id, now=T0 + 1)
        self.assertEqual(raised.exception.reason, 'system_key')
        self.assertTrue(keys.verify(system.raw, T0 + 2).system, 'still working after the refused revoke')
        self.assertIsNone(next(k for k in keys.list() if k.system).revoked_at)
        with self.assertRaises(auth.ApiKeyRejected) as reserved:
            keys.issue('board-agent', 'editor', {'all': True}, None, now=T0)
        self.assertEqual(reserved.exception.reason, 'name_reserved')
        self.assertEqual(len(keys.list()), 1)
        fresh = auth.ApiKeyStore(self.hubdb())
        with self.assertRaises(auth.ApiKeyRejected):
            fresh.issue('board-agent', 'requester', {'projects': ['p']}, 5, now=T0)

    def test_an_owner_key_cannot_pass_as_the_system_principal(self):
        keys = auth.ApiKeyStore(self.hubdb())
        owner = keys.issue('looks-like-a-board', 'editor', {'all': True}, None, now=T0)
        self.assertFalse(keys.verify(owner.raw, T0).system)


# ===========================================================================================
# audit
# ===========================================================================================

SPEC_EVENTS = {
    'login_success': {'delay_seconds': 0}, 'login_failure': {'delay_seconds': 4}, 'logout': {'delay_seconds': 0},
    'password_changed': {'source': 'cli', 'invalidated_sessions': 3},
    'api_key_issued': {'key_id': 'abcdefghij', 'name': 'n', 'grade': 'editor', 'scope': {'all': True}, 'expires_at': T0},
    'api_key_revoked': {'key_id': 'abcdefghij', 'name': 'n', 'grade': 'requester', 'scope': {'all': True}, 'expires_at': None},
    'terminal_attach': {'epoch': 2, 'previous_epoch': 1}, 'terminal_takeover': {'epoch': 3, 'previous_epoch': 2},
    'terminal_release': {'epoch': 3}, 'terminal_timeout': {'epoch': 3}, 'terminal_closed': {'epoch': 3},
    'board_restart': {'channel': 'agent', 'pid': 99, 'reason': 'user'}, 'board_start': {'channel': 'shell', 'pid': 100},
    'board_stop': {'channel': 'agent', 'pid': 99, 'reason': 'hub_stop'},
    'web_control': {'project_id': 'p1', 'action': 'pause', 'result': 'ok'},
}


class AuditTests(AuthCase):
    def log(self, **kwargs) -> auth.AuditLog:
        return auth.AuditLog(self.hubdb(), **kwargs)

    def test_every_spec_event_is_accepted_with_its_fields_and_stored_as_a_plain_row(self):
        log = self.log()
        for i, (event, detail) in enumerate(SPEC_EVENTS.items()):
            log.record(event, 'owner', '10.8.0.2', detail, now=T0 + i)
        rows = self.sql('SELECT id, at, event, actor, client_addr, detail_json FROM audit ORDER BY id')
        self.assertEqual([r[2] for r in rows], list(SPEC_EVENTS))
        self.assertEqual([r[0] for r in rows], list(range(1, len(SPEC_EVENTS) + 1)))
        for (rid, at, event, actor, addr, detail_json), (want_event, want_detail) in zip(rows, SPEC_EVENTS.items()):
            self.assertEqual((actor, addr, json.loads(detail_json)), ('owner', '10.8.0.2', want_detail))
        page = log.list_audit(1)
        self.assertEqual([e.event for e in page.entries], list(reversed(list(SPEC_EVENTS))), 'newest first')
        self.assertEqual(page.total, len(SPEC_EVENTS))
        self.assertEqual(page.entries[0].detail, SPEC_EVENTS['web_control'])
        self.assertEqual(self.sql("SELECT sql FROM sqlite_master WHERE name = 'audit'")[0][0].count('detail_json'), 1)

    def test_a_project_scoped_key_event_is_recordable_with_the_spec_scope_shape(self):
        # SPEC 감사 기록: api_key_issued/revoked detail = key_id, name, grade, scope, expires_at, and
        # scope is {"all": true} or {"projects": [id, ...]} (API key section).
        log = self.log()
        scope = {'projects': ['p1', 'p2']}
        for event in ('api_key_issued', 'api_key_revoked'):
            try:
                log.record(event, 'owner', '10.8.0.2', {'key_id': 'abcdefghij', 'name': 'n', 'grade': 'requester',
                                                         'scope': scope, 'expires_at': T0 + DAY}, now=T0)
            except auth.AuditRejected as exc:
                self.fail(f'{event} with a {{"projects": [...]}} scope was refused: {exc}')
        self.assertEqual([e.detail['scope'] for e in log.list_audit(1).entries], [scope, scope])

    def test_unknown_events_and_non_spec_fields_are_refused_and_nothing_is_stored(self):
        log = self.log()
        refused = [
            ('login', {}), ('password', {}), ('LOGIN_SUCCESS', {}), ('', {}), (None, {}), ('terminal_input', {'data': 'ls'}),
            ('login_success', {'password': PASSWORD}), ('login_failure', {'attempted_password': 'x'}),
            ('login_success', {'delay_seconds': 0, 'token': 't'}), ('password_changed', {'source': 'cli', 'password': 'p'}),
            ('api_key_issued', {'key_id': 'k', 'secret': 'wbk_x'}), ('api_key_issued', {'raw_key': 'wbk_x'}),
            ('api_key_issued', {'key': 'wbk_x'}), ('api_key_revoked', {'secret_hash': 'h'}),
            ('terminal_attach', {'epoch': 1, 'input': 'rm -rf'}), ('terminal_closed', {'output': 'x'}),
            ('web_control', {'project_id': 'p', 'body': 'request body'}), ('board_start', {'command': 'x'}),
            ('login_success', 'not a dict'), ('login_success', None), ('login_success', ['delay_seconds']),
        ]
        for event, detail in refused:
            with self.subTest(event=event, detail=str(detail)[:30]):
                with self.assertRaises(auth.AuditRejected):
                    log.record(event, 'owner', None, detail, now=T0)
        for detail in ({'name': 'x' * 257}, {'name': object()}, {'name': b'bytes'}):
            with self.assertRaises(auth.AuditRejected):
                log.record('api_key_issued', 'owner', None, {**detail}, now=T0)
        with self.assertRaises(auth.AuditRejected):
            log.record('logout', 'x' * 300, None, {}, now=T0)
        with self.assertRaises(auth.AuditRejected):
            log.record('logout', 'owner', 7, {}, now=T0)
        self.assertEqual(self.sql('SELECT count(*) FROM audit'), [(0,)])

    def test_audit_rows_of_a_whole_session_hold_no_secret(self):
        store = self.passwords()
        db = self.hubdb()
        generation = store.set_password(PASSWORD)
        sessions = auth.SessionStore(db, store)
        keys = auth.ApiKeyStore(db)
        log = auth.AuditLog(db)
        issued = sessions.create('10.8.0.2', 'ua', T0, generation=generation)
        key = keys.issue('audited', 'editor', {'all': True}, 5, now=T0)
        info = keys.list()[0]
        log.record('login_success', 'owner', '10.8.0.2', {'delay_seconds': 0}, now=T0)
        log.record('api_key_issued', 'owner', '10.8.0.2',
                   {'key_id': info.key_id, 'name': info.name, 'grade': info.grade, 'scope': info.scope,
                    'expires_at': info.expires_at}, now=T0)
        log.record('password_changed', 'cli', None, {'source': 'cli', 'invalidated_sessions': 1}, now=T0)
        db.close()
        text = repr(self.sql('SELECT * FROM audit')).encode()
        for needle in (issued.token, issued.csrf, key.raw, key.raw[15:]):
            self.assertNotIn(needle.encode(), text)
        self.assertAbsent(text, PASSWORD, 'audit rows')

    def test_only_the_newest_fifty_thousand_rows_are_kept(self):
        self.assertEqual(auth.AUDIT_LIMIT, 50_000)
        db = self.hubdb()
        log = auth.AuditLog(db)
        self.assertEqual(log.limit, 50_000)
        raw = sqlite3.connect(self.db_path)
        try:
            raw.execute('BEGIN')
            raw.executemany("INSERT INTO audit (at, event, actor, client_addr, detail_json) VALUES (?, 'logout', 'owner', NULL, '{}')",
                            [(float(i),) for i in range(49_998)])
            raw.execute('COMMIT')
        finally:
            raw.close()
        for i in range(5):
            log.record('login_failure', 'owner', '10.8.0.2', {'delay_seconds': i}, now=T0 + i)
        self.assertEqual(self.sql('SELECT count(*), min(id), max(id) FROM audit'), [(50_000, 4, 50_003)])
        newest = log.list_audit(1).entries[0]
        self.assertEqual((newest.event, newest.detail), ('login_failure', {'delay_seconds': 4}))
        self.assertEqual(log.list_audit(1).total, 50_000)

    def test_small_limit_pages_and_odd_page_numbers(self):
        log = self.log(limit=7)
        for i in range(12):
            log.record('logout', 'owner', None, {}, now=T0 + i)
        self.assertEqual(self.sql('SELECT min(at), max(at) FROM audit'), [(T0 + 5, T0 + 11)])
        page1, page2, page3 = (log.list_audit(n, page_size=3) for n in (1, 2, 3))
        self.assertEqual([e.at - T0 for e in page1.entries], [11, 10, 9])
        self.assertEqual([e.at - T0 for e in page2.entries], [8, 7, 6])
        self.assertEqual([e.at - T0 for e in page3.entries], [5])
        self.assertEqual(log.list_audit(0, page_size=3).entries, page1.entries)
        self.assertEqual(log.list_audit(-4, page_size=3).entries, page1.entries)
        self.assertEqual(log.list_audit(99, page_size=3).entries, [])

    def test_concurrent_recorders_lose_no_row(self):
        log = self.log()
        errors = []

        def work(n):
            try:
                for i in range(25):
                    log.record('login_failure', f'w{n}', None, {'delay_seconds': i}, now=T0 + i)
            except BaseException as exc:
                errors.append(repr(exc))
        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(self.sql('SELECT count(*), count(DISTINCT id) FROM audit'), [(200, 200)])


# ===========================================================================================
# hub.sqlite3 file safety
# ===========================================================================================

class HubDbFileTests(AuthCase):
    def test_a_fresh_db_is_private_wal_v1_with_the_four_spec_tables(self):
        db = self.hubdb()
        self.assertEqual(mode_of(self.db_path), 0o600)
        self.assertEqual(db.query('PRAGMA journal_mode'), [('wal',)])
        self.assertEqual(db.query('PRAGMA user_version'), [(1,)])
        tables = {r[0]: r for r in self.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(set(tables), {'sessions', 'api_keys', 'login_state', 'audit', 'sqlite_sequence'})
        columns = {t: [r[1] for r in self.sql(f'PRAGMA table_info({t})')] for t in ('sessions', 'api_keys', 'login_state', 'audit')}
        self.assertEqual(columns['sessions'], ['token_hash', 'created_at', 'expires_at', 'password_generation',
                                               'csrf_token', 'client_addr', 'user_agent'])
        self.assertEqual(columns['api_keys'], ['key_id', 'name', 'grade', 'scope_json', 'secret_hash', 'created_at',
                                               'expires_at', 'revoked_at', 'last_used_at', 'system'])
        self.assertEqual(columns['audit'], ['id', 'at', 'event', 'actor', 'client_addr', 'detail_json'])
        self.assertEqual(columns['login_state'][0], 'key')
        for sidecar in ('hub.sqlite3-wal', 'hub.sqlite3-shm'):
            if (self.state / sidecar).exists():
                self.assertEqual(mode_of(self.state / sidecar), 0o600, sidecar)

    def test_reopening_keeps_rows_and_does_not_recreate_the_schema(self):
        first = auth.HubDb.open(self.state)
        auth.AuditLog(first).record('logout', 'owner', None, {}, now=T0)
        first.close()
        second = self.hubdb()
        self.assertEqual(second.query('SELECT count(*) FROM audit'), [(1,)])

    def test_unsafe_or_foreign_db_files_are_refused_and_left_alone(self):
        self.state.parent.mkdir(parents=True, mode=0o700)
        self.state.mkdir(mode=0o700)
        victim = self.base / 'victim.db'
        victim.write_bytes(b'precious')
        os.chmod(victim, 0o600)
        os.symlink(victim, self.db_path)
        with self.assertRaises(auth.HubDbError):
            auth.HubDb.open(self.state)
        self.assertEqual(victim.read_bytes(), b'precious')
        os.unlink(self.db_path)

        fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.db_path, 0o644)
        with self.assertRaises(auth.HubDbError):
            auth.HubDb.open(self.state)
        self.assertEqual(mode_of(self.db_path), 0o644, 'refused, not silently chmod-ed')
        os.chmod(self.db_path, 0o600)

        link = self.state / 'second-name'
        os.link(self.db_path, link)
        with self.assertRaises(auth.HubDbError):
            auth.HubDb.open(self.state)
        os.unlink(link)

        connection = sqlite3.connect(self.db_path)
        connection.execute('CREATE TABLE stranger (x)')
        connection.commit()
        connection.close()
        with self.assertRaises(auth.HubDbError):
            auth.HubDb.open(self.state)
        os.unlink(self.db_path)

        connection = sqlite3.connect(self.db_path)
        connection.execute('PRAGMA user_version = 9')
        connection.commit()
        connection.close()
        with self.assertRaises(auth.HubDbError):
            auth.HubDb.open(self.state)


# ===========================================================================================
# wb hub passwd (CLI)
# ===========================================================================================

class CliSandbox(SpawnSandbox):
    def __init__(self, case, *, autostart: bool = False):
        super().__init__(case)
        self.env['PYTHONPATH'] = absolute_pythonpath()
        if not autostart:
            self.env['WORKBENCH_HUB_AUTOSTART'] = '0'
        # a short private runtime dir, owned and removed with the sandbox
        self.runtime.chmod(0o700)

    @property
    def auth_path(self) -> Path:
        return self.state / 'auth.json'

    def auth(self) -> dict:
        return json.loads(self.auth_path.read_text())

    def run_cli(self, *args: str, data: bytes | None = b'', env: dict | None = None, umask: int | None = None
                ) -> subprocess.CompletedProcess:
        return subprocess.run(self.wb_argv('hub', 'passwd', *args), env=env or self.env, cwd=self.base, input=data,
                              stdin=None if data is not None else subprocess.DEVNULL, capture_output=True, timeout=60,
                              preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)

    def run_tty(self, answers: list[str], timeout: float = 60) -> tuple[int, bytes]:
        child, master = pty.fork()
        if child == 0:  # pragma: no cover
            try:
                os.chdir(self.base)
                os.execve(self.wb_argv()[0], self.wb_argv('hub', 'passwd'), self.env)
            finally:
                os._exit(127)
        output, pending, answered_at = b'', list(answers), 0
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.2)[0]:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                # answer only a *new* prompt: the bare newline getpass prints after an answer must not
                # trigger the next answer early (termios would flush it away and hang the child)
                if pending and output[answered_at:].strip().endswith(b':'):
                    os.write(master, pending.pop(0).encode() + b'\n')
                    answered_at = len(output)
        finally:
            os.close(master)
            timed_out = time.monotonic() >= deadline
            if timed_out:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child, signal.SIGKILL)
            _, status = os.waitpid(child, 0)
        self.case.assertFalse(timed_out, 'passwd on a tty did not finish')
        return (os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1), output


class PasswdCliTests(unittest.TestCase):
    def setUp(self):
        self.box = CliSandbox(self)

    def assertNoPassword(self, password: str, *blobs: bytes) -> None:
        for blob in blobs:
            for needle in needles_for(password):
                self.assertNotIn(needle, blob)

    def test_stdin_writes_a_verified_scrypt_record_and_leaks_nothing(self):
        result = self.box.run_cli('--stdin', data=PASSWORD.encode() + b'\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.box.auth()
        salt = base64.b64decode(record['salt'], validate=True)
        self.assertEqual(base64.b64decode(record['hash'], validate=True), scrypt_bytes(PASSWORD, salt))
        self.assertEqual([record[k] for k in ('schema_version', 'kdf', 'n', 'r', 'p', 'dklen')], [1, 'scrypt', 32768, 8, 1, 32])
        self.assertGreaterEqual(record['generation'], 1)
        self.assertEqual(mode_of(self.box.auth_path), 0o600)
        self.assertEqual(mode_of(self.box.state), 0o700)
        self.assertNoPassword(PASSWORD, result.stdout, result.stderr, self.box.auth_path.read_bytes())
        for path in self.box.state.rglob('*'):
            if path.is_file():
                self.assertNoPassword(PASSWORD, path.read_bytes())
        self.assertFalse((self.box.state / 'hub.sqlite3').exists(), 'the CLI never writes hub.sqlite3')
        again = self.box.run_cli('--stdin', data=OTHER_PASSWORD.encode() + b'\r\n')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.box.auth()['generation'], record['generation'] + 1)
        second = self.box.auth()
        self.assertEqual(base64.b64decode(second['hash']), scrypt_bytes(OTHER_PASSWORD, base64.b64decode(second['salt'])),
                         'CRLF is stripped as a line end')

    def test_stdin_keeps_the_password_exactly_as_typed(self):
        padded = '  padded passphrase  '
        self.assertEqual(self.box.run_cli('--stdin', data=padded.encode() + b'\n').returncode, 0)
        record = self.box.auth()
        self.assertEqual(base64.b64decode(record['hash']), scrypt_bytes(padded, base64.b64decode(record['salt'])))

    def test_refused_input_exits_1_leaves_bytes_unchanged_and_never_echoes_the_input(self):
        self.assertEqual(self.box.run_cli('--stdin', data=PASSWORD.encode() + b'\n').returncode, 0)
        before = self.box.auth_path.read_bytes()
        before_ino = os.stat(self.box.auth_path).st_ino
        for data in (b'', b'\n', b'S3cr3t!\n', b'123456789\n', b'x' * 1025 + b'\n', b'\xff\xfe\xfd\xfc\xfb\xfa\xf9\xf8\xf7\xf6\n'):
            with self.subTest(data=data[:12]):
                result = self.box.run_cli('--stdin', data=data)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.box.auth_path.read_bytes(), before)
                self.assertEqual(os.stat(self.box.auth_path).st_ino, before_ino, 'not even rewritten')
                if data.strip() and len(data) < 100:
                    self.assertNotIn(data.strip(), result.stdout + result.stderr)
        self.assertEqual(sorted(p.name for p in self.box.state.iterdir()), ['auth.json', 'auth.lock'])

    def test_refusal_on_a_fresh_install_creates_no_auth_file(self):
        result = self.box.run_cli('--stdin', data=b'S3cr3t!\n')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.box.auth_path.exists())

    def test_without_stdin_flag_and_without_a_terminal_exit_2_and_nothing_is_created(self):
        env = {**self.box.env, 'PASSWORD': PASSWORD, 'WB_HUB_PASSWORD': PASSWORD, 'WB_PASSWORD': PASSWORD}
        for label, data in (('pipe', PASSWORD.encode() + b'\n' + PASSWORD.encode() + b'\n'), ('devnull', None)):
            with self.subTest(label):
                result = self.box.run_cli(data=data, env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b'--stdin', result.stderr)
                self.assertFalse(self.box.auth_path.exists())
                self.assertFalse(self.box.state.exists(), 'not even the state dir')
                self.assertNoPassword(PASSWORD, result.stdout, result.stderr)

    def test_the_password_is_not_accepted_from_argv_or_environment(self):
        for args in (('--password', PASSWORD), (PASSWORD,)):
            result = subprocess.run(self.box.wb_argv('hub', 'passwd', *args), env={**self.box.env, 'PASSWORD': PASSWORD},
                                    cwd=self.box.base, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(self.box.auth_path.exists())
        self.assertFalse(self.box.state.exists())

    def test_tty_two_matching_entries_set_the_password_without_echo(self):
        status, output = self.box.run_tty([PASSWORD, PASSWORD])
        self.assertEqual(status, 0, output)
        self.assertNoPassword(PASSWORD, output)
        record = self.box.auth()
        self.assertEqual(base64.b64decode(record['hash']), scrypt_bytes(PASSWORD, base64.b64decode(record['salt'])))
        self.assertEqual(mode_of(self.box.auth_path), 0o600)
        first = record['generation']
        status, output = self.box.run_tty([OTHER_PASSWORD, OTHER_PASSWORD])
        self.assertEqual(status, 0, output)
        self.assertEqual(self.box.auth()['generation'], first + 1)

    def test_tty_mismatched_or_short_entries_are_refused_and_change_nothing(self):
        status, output = self.box.run_tty([PASSWORD, PASSWORD + 'x'])
        self.assertNotEqual(status, 0, output)
        self.assertFalse(self.box.auth_path.exists(), 'mismatch on a fresh install writes nothing')
        self.assertNoPassword(PASSWORD, output)
        status, _ = self.box.run_tty([PASSWORD, PASSWORD])
        self.assertEqual(status, 0)
        before = self.box.auth_path.read_bytes()
        status, output = self.box.run_tty([OTHER_PASSWORD, PASSWORD])
        self.assertNotEqual(status, 0, output)
        self.assertEqual(self.box.auth_path.read_bytes(), before)
        self.assertNoPassword(OTHER_PASSWORD, output)
        status, output = self.box.run_tty(['S3cr3t!', 'S3cr3t!'])
        self.assertEqual(status, 1, output)
        self.assertEqual(self.box.auth_path.read_bytes(), before)
        self.assertNotIn(b'S3cr3t!', output)

    def test_the_cli_obeys_the_umask_for_modes_and_leaves_the_hub_db_alone(self):
        for umask in (0o000, 0o022):
            with self.subTest(umask=oct(umask)):
                box = CliSandbox(self)
                result = box.run_cli('--stdin', data=PASSWORD.encode() + b'\n', umask=umask)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(mode_of(box.auth_path), 0o600)
                self.assertEqual(mode_of(box.state), 0o700)
                self.assertEqual(mode_of(box.state.parent), 0o700)
                self.assertEqual(mode_of(box.state / 'auth.lock'), 0o600)

    def test_a_cli_password_change_invalidates_hub_sessions_and_never_touches_the_db(self):
        box = self.box
        self.assertEqual(box.run_cli('--stdin', data=PASSWORD.encode() + b'\n').returncode, 0)
        store = auth.PasswordStore(box.state)
        db = auth.HubDb.open(box.state)
        try:
            sessions = auth.SessionStore(db, store)
            issued = sessions.create('10.8.0.2', 'ua', T0, generation=store.verify(PASSWORD))
            self.assertIsNotNone(sessions.verify(issued.token, T0 + 1))
            db.close()
            digest_before = hashlib.sha256((box.state / 'hub.sqlite3').read_bytes()).hexdigest()
            self.assertEqual(box.run_cli('--stdin', data=OTHER_PASSWORD.encode() + b'\n').returncode, 0)
            self.assertEqual(hashlib.sha256((box.state / 'hub.sqlite3').read_bytes()).hexdigest(), digest_before,
                             'passwd must not write hub.sqlite3 (Hub is the single writer)')
            reopened = auth.HubDb.open(box.state)
            try:
                self.assertIsNone(auth.SessionStore(reopened, store).verify(issued.token, T0 + 1))
            finally:
                reopened.close()
        finally:
            db.close()

    def test_the_cli_does_not_follow_a_symlinked_auth_file(self):
        box = self.box
        box.state.parent.mkdir(parents=True, mode=0o700)
        box.state.mkdir(mode=0o700)
        victim = box.base / 'victim'
        victim.write_text('keep me')
        os.symlink(victim, box.auth_path)
        box.run_cli('--stdin', data=PASSWORD.encode() + b'\n')
        self.assertEqual(victim.read_text(), 'keep me')

    def test_passwd_never_starts_a_hub_even_when_auto_start_is_enabled(self):
        box = CliSandbox(self, autostart=True)
        self.assertNotIn('WORKBENCH_HUB_AUTOSTART', box.env)
        self.assertEqual(box.run_cli('--stdin', data=PASSWORD.encode() + b'\n').returncode, 0)
        status, _ = box.run_tty([OTHER_PASSWORD, 'mismatch-' + OTHER_PASSWORD])
        self.assertNotEqual(status, 0)
        self.assertEqual(box.run_cli(data=b'').returncode, 2)
        time.sleep(0.5)
        self.assertEqual(box.hub_pids(), [])
        self.assertFalse(box.lock.exists())
        self.assertFalse(box.record_path.exists())
        self.assertFalse(box.socket_dir.exists())


if __name__ == '__main__':
    unittest.main()
