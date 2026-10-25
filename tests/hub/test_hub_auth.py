"""Auth core (TI-02, seam T1a): ``workbench.hub.auth`` API plus the ``auth.json``/``hub.sqlite3`` files.

Expected values come from the SPEC numbers (scrypt n=2^15, r=8, p=1, dklen=32; 24 h; f(n);
50,000 audit rows; key format) and from hashes the test computes itself with
``hashlib.scrypt``/``hashlib.sha256``. Files are read back with plain ``json``/``os``/stdlib
``sqlite3``, not through the product reader. Clocks are injected as plain ``now`` floats.
Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import auth  # noqa: E402

SCRYPT = {'n': 32768, 'r': 8, 'p': 1, 'dklen': 32}
PASSWORD = 'correct horse battery'


def scrypt_b64(password: str, salt_b64: str) -> str:
    salt = base64.b64decode(salt_b64)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=1, dklen=32,
                            maxmem=64 * 1024 * 1024)
    return base64.b64encode(digest).decode('ascii')


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class AuthCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-auth-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state' / 'omp-workbench' / 'hub'
        self.auth_path = self.state / 'auth.json'

    def auth_json(self) -> dict:
        return json.loads(self.auth_path.read_text())


class SetPasswordTests(AuthCase):
    def test_set_password_writes_a_private_scrypt_record_and_counts_generations(self):
        store = auth.PasswordStore(self.state)
        first = store.set_password(PASSWORD)
        record = self.auth_json()
        self.assertEqual(set(record), {'schema_version', 'kdf', 'n', 'r', 'p', 'dklen', 'salt', 'hash',
                                       'generation', 'changed_at'})
        self.assertEqual((record['schema_version'], record['kdf']), (1, 'scrypt'))
        self.assertEqual({key: record[key] for key in SCRYPT}, SCRYPT)
        self.assertEqual(len(base64.b64decode(record['salt'])), 16)
        self.assertEqual(record['hash'], scrypt_b64(PASSWORD, record['salt']))
        self.assertNotIn(PASSWORD, self.auth_path.read_text())
        self.assertEqual(record['generation'], first)
        self.assertEqual(mode_of(self.auth_path), 0o600)
        self.assertEqual(mode_of(self.state), 0o700)
        self.assertEqual(mode_of(self.state / 'auth.lock'), 0o600)

        second = store.set_password('another long password')
        again = self.auth_json()
        self.assertEqual((second, again['generation']), (first + 1, first + 1))
        self.assertNotEqual(again['salt'], record['salt'], 'every write draws a new salt')
        self.assertEqual(store.generation(), first + 1)
        self.assertFalse((self.state / 'hub.sqlite3').exists(), 'the password store never opens the Hub DB')

    def test_length_rule_rejects_and_leaves_the_file_unchanged(self):
        store = auth.PasswordStore(self.state)
        store.set_password(PASSWORD)
        before = self.auth_path.read_bytes()
        for bad, reason in (('', 'password_too_short'), ('x' * 9, 'password_too_short'),
                            ('x' * 1025, 'password_too_long')):
            with self.subTest(len(bad)):
                with self.assertRaises(auth.PasswordRejected) as caught:
                    store.set_password(bad)
                self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.auth_path.read_bytes(), before)
        store.set_password('x' * 10)
        store.set_password('x' * 1024)


class VerifyPasswordTests(AuthCase):
    def test_verify_returns_the_matching_generation_and_rejects_others(self):
        store = auth.PasswordStore(self.state)
        self.assertIsNone(store.verify(PASSWORD), 'no password set: nothing verifies')
        generation = store.set_password(PASSWORD)
        self.assertEqual(store.verify(PASSWORD), generation)
        self.assertIsNone(store.verify(PASSWORD + ' '))
        self.assertIsNone(store.verify(''))

    def test_a_corrupt_or_unsafe_file_is_auth_invalid_not_no_password(self):
        store = auth.PasswordStore(self.state)
        store.set_password(PASSWORD)
        good = self.auth_json()
        variants = {
            'not json': b'{',
            'empty': b'',
            'array': b'[]',
            'missing hash': json.dumps({k: v for k, v in good.items() if k != 'hash'}).encode(),
            'weaker n': json.dumps(dict(good, n=1024)).encode(),
            'other kdf': json.dumps(dict(good, kdf='sha256')).encode(),
            'zero generation': json.dumps(dict(good, generation=0)).encode(),
            'bool generation': json.dumps(dict(good, generation=True)).encode(),
            'short hash': json.dumps(dict(good, hash=base64.b64encode(b'x' * 8).decode())).encode(),
            'extra key': json.dumps(dict(good, note='x')).encode(),
        }
        for label, payload in variants.items():
            with self.subTest(label):
                self.auth_path.write_bytes(payload)
                os.chmod(self.auth_path, 0o600)
                for call in (store.load, store.generation, lambda: store.verify(PASSWORD)):
                    with self.assertRaises(auth.AuthInvalid):
                        call()
        self.auth_path.write_text(json.dumps(good))
        os.chmod(self.auth_path, 0o644)
        with self.assertRaises(auth.AuthInvalid):
            store.verify(PASSWORD)
        os.unlink(self.auth_path)
        target = self.state / 'elsewhere.json'
        target.write_text(json.dumps(good))
        os.chmod(target, 0o600)
        self.auth_path.symlink_to(target)
        with self.assertRaises(auth.AuthInvalid):
            store.generation()

    def test_rewriting_a_corrupt_file_recovers_with_a_fresh_generation(self):
        store = auth.PasswordStore(self.state)
        old = store.set_password(PASSWORD)
        self.auth_path.write_bytes(b'garbage')
        new = store.set_password('a brand new password')
        self.assertNotEqual(new, old)
        self.assertEqual(store.verify('a brand new password'), new)


T0 = 1_800_000_000.0
DAY = 86400.0


class DbCase(AuthCase):
    def setUp(self):
        super().setUp()
        self.passwords = auth.PasswordStore(self.state)
        self.generation = self.passwords.set_password(PASSWORD)
        self.db = auth.HubDb.open(self.state)
        self.addCleanup(self.db.close)
        self.db_path = self.state / 'hub.sqlite3'

    def rows(self, sql: str, *args) -> list[tuple]:
        import sqlite3
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def all_db_bytes(self) -> bytes:
        return b''.join(path.read_bytes() for path in self.state.iterdir() if path.name.startswith('hub.sqlite3'))


class HubDbTests(DbCase):
    def test_hub_db_is_a_private_wal_database_with_the_v1_tables(self):
        self.assertEqual(mode_of(self.db_path), 0o600)
        self.assertEqual(self.rows('PRAGMA journal_mode'), [('wal',)])
        tables = {name for (name,) in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({'sessions', 'api_keys', 'login_state', 'audit'} <= tables, tables)
        columns = {name for _, name, *_ in self.rows('PRAGMA table_info(sessions)')}
        self.assertEqual(columns, {'token_hash', 'created_at', 'expires_at', 'password_generation', 'csrf_token',
                                   'client_addr', 'user_agent'})
        for path in self.state.iterdir():
            if path.name.startswith('hub.sqlite3'):
                self.assertEqual(mode_of(path), 0o600, path.name)

    def test_an_unsafe_hub_db_path_is_refused(self):
        self.db.close()
        os.chmod(self.db_path, 0o644)
        with self.assertRaises(auth.HubDbError):
            auth.HubDb.open(self.state)
        os.chmod(self.db_path, 0o600)
        moved = self.state / 'real.sqlite3'
        os.rename(self.db_path, moved)
        self.db_path.symlink_to(moved)
        with self.assertRaises(auth.HubDbError):
            auth.HubDb.open(self.state)


class SessionTests(DbCase):
    def test_a_session_verifies_until_exactly_24_hours_after_login(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        issued = sessions.create('10.0.0.5', 'Firefox', T0, generation=self.generation)
        token = issued.token
        self.assertEqual(len(base64.urlsafe_b64decode(token + '=')), 32)
        self.assertEqual(len(base64.urlsafe_b64decode(issued.csrf + '=')), 32)
        self.assertEqual(issued.expires_at, T0 + DAY)
        stored = self.rows('SELECT token_hash, created_at, expires_at, password_generation, csrf_token, '
                           'client_addr, user_agent FROM sessions')
        self.assertEqual(stored, [(hashlib.sha256(token.encode()).hexdigest(), T0, T0 + DAY, self.generation,
                                   issued.csrf, '10.0.0.5', 'Firefox')])
        self.assertNotIn(token.encode(), self.all_db_bytes(), 'the raw token is never stored')

        session = sessions.verify(token, T0 + DAY - 0.001)
        self.assertIsNotNone(session)
        self.assertEqual((session.csrf, session.expires_at, session.client_addr), (issued.csrf, T0 + DAY, '10.0.0.5'))
        self.assertIsNone(sessions.verify(token, T0 + DAY), 'expiry is absolute: exactly 24 h is expired')
        self.assertIsNone(sessions.verify(token, T0 + 2 * DAY))
        self.assertIsNone(sessions.verify('not-a-token', T0))
        self.assertIsNone(sessions.verify(token[:-1] + ('A' if token[-1] != 'A' else 'B'), T0))

    def test_delete_ends_one_session(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        one = sessions.create('a', 'ua', T0, generation=self.generation)
        two = sessions.create('b', 'ua', T0, generation=self.generation)
        self.assertNotEqual(one.token, two.token)
        sessions.delete(one.token)
        self.assertIsNone(sessions.verify(one.token, T0 + 1))
        self.assertIsNotNone(sessions.verify(two.token, T0 + 1))


class GenerationTests(DbCase):
    def test_a_password_change_rejects_every_existing_session(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        tokens = [sessions.create(f'10.0.0.{i}', 'ua', T0, generation=self.generation).token for i in range(3)]
        self.passwords.set_password('the replacement password')  # e.g. 'wb hub passwd' from another process
        for token in tokens:
            self.assertIsNone(sessions.verify(token, T0 + 1))

    def test_a_login_verified_before_a_change_gets_a_session_that_is_already_stale(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        verified = self.passwords.verify(PASSWORD)
        self.passwords.set_password('changed while the login was in flight')
        stale = sessions.create('10.0.0.9', 'ua', T0, generation=verified)
        self.assertIsNone(sessions.verify(stale.token, T0 + 1))

    def test_a_corrupt_password_file_rejects_every_session(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        token = sessions.create('a', 'ua', T0, generation=self.generation).token
        good = self.auth_path.read_bytes()
        self.auth_path.write_bytes(b'{broken')
        self.assertIsNone(sessions.verify(token, T0 + 1))
        self.auth_path.write_bytes(good)
        self.assertIsNotNone(sessions.verify(token, T0 + 1))

    def test_delete_all_except_keeps_only_the_given_session(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        keep, *others = [sessions.create(str(i), 'ua', T0, generation=self.generation).token for i in range(4)]
        self.assertEqual(sessions.delete_all_except(keep), 3)
        self.assertIsNotNone(sessions.verify(keep, T0 + 1))
        for token in others:
            self.assertIsNone(sessions.verify(token, T0 + 1))
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(1,)])

    def test_reissue_after_a_web_password_change_keeps_only_the_current_login(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        current = sessions.create('10.0.0.1', 'Firefox', T0, generation=self.generation)
        other = sessions.create('10.0.0.2', 'Chromium', T0 + 5, generation=self.generation)
        new_generation = self.passwords.set_password('the new web password')
        reissued = sessions.reissue(current.token, T0 + 60, expected_generation=self.generation)
        self.assertEqual(reissued.invalidated, 1)
        self.assertNotEqual(reissued.session.token, current.token)
        self.assertNotEqual(reissued.session.csrf, current.csrf)
        self.assertEqual(reissued.session.expires_at, T0 + DAY, 'reissue never extends the 24 h login')
        session = sessions.verify(reissued.session.token, T0 + 61)
        self.assertEqual((session.password_generation, session.client_addr, session.user_agent),
                         (new_generation, '10.0.0.1', 'Firefox'))
        self.assertIsNone(sessions.verify(current.token, T0 + 61))
        self.assertIsNone(sessions.verify(other.token, T0 + 61))
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(1,)])
        self.assertIsNone(sessions.reissue(current.token, T0 + 62), 'the old token is gone')
        self.assertIsNone(sessions.reissue(reissued.session.token, T0 + DAY), 'an expired login is not reissued')

    def test_reissue_never_revives_a_session_an_earlier_change_already_invalidated(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        stale = sessions.create('10.0.0.1', 'ua', T0, generation=self.generation)
        self.passwords.set_password('cli reset number one')       # stale is invalid from here on
        self.passwords.set_password('web change number two')
        self.assertIsNone(sessions.reissue(stale.token, T0 + 1), 'two changes since its login')
        self.assertIsNone(sessions.reissue(stale.token, T0 + 1, expected_generation=self.generation),
                          'another change slipped in between the web change and the reissue')
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(1,)], 'a refusal deletes nothing')

    def test_reissue_requires_the_session_to_carry_the_expected_generation(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        current = sessions.create('10.0.0.1', 'ua', T0, generation=self.generation)
        new_generation = self.passwords.set_password('the new web password')
        self.assertIsNone(sessions.reissue(current.token, T0 + 1, expected_generation=new_generation))
        reissued = sessions.reissue(current.token, T0 + 1, expected_generation=self.generation)
        self.assertEqual(sessions.verify(reissued.session.token, T0 + 2).password_generation, new_generation)


class LoginDelayTests(DbCase):
    def test_delay_function_follows_the_spec_table(self):
        expected = {0: 0, 1: 0, 2: 0, 3: 0, 4: 1, 5: 2, 6: 4, 7: 8, 8: 16, 9: 30, 10: 30, 50: 30}
        self.assertEqual({n: auth.delay_seconds(n) for n in expected}, expected)

    def test_delay_is_the_larger_of_the_address_and_the_global_third(self):
        throttle = auth.LoginThrottle(self.db)
        for addr, count in (('10.0.0.1', 3), ('10.0.0.2', 5), ('10.0.0.3', 4)):
            for _ in range(count):
                throttle.record_failure(addr)
        # global = 12 -> f(12 / 3) = f(4) = 1 s
        self.assertEqual([throttle.delay_for(addr) for addr in ('10.0.0.1', '10.0.0.2', '10.0.0.3', '10.9.9.9')],
                         [1, 2, 1, 1])
        throttle.record_success('10.0.0.1')
        self.assertEqual([throttle.delay_for(addr) for addr in ('10.0.0.1', '10.0.0.2', '10.0.0.3', '10.9.9.9')],
                         [0, 2, 1, 0], 'a success clears that address and the global count only')
        self.db.close()
        reopened = auth.HubDb.open(self.state)
        self.addCleanup(reopened.close)
        again = auth.LoginThrottle(reopened)
        self.assertEqual([again.delay_for(addr) for addr in ('10.0.0.1', '10.0.0.2', '10.0.0.3')], [0, 2, 1],
                         'counts survive a Hub restart')
        for _ in range(20):
            again.record_failure('10.0.0.2')
        self.assertEqual(again.delay_for('10.0.0.2'), 30)

    def test_check_returns_the_remaining_wait_without_sleeping(self):
        import time as real_time
        throttle = auth.LoginThrottle(self.db)
        self.assertEqual(throttle.check('10.0.0.1', T0), 0)
        for _ in range(5):
            throttle.record_failure('10.0.0.1', now=T0)       # f(5) = 2 s from the last failure
        started = real_time.monotonic()
        self.assertEqual(throttle.check('10.0.0.1', T0), 2)
        self.assertEqual(throttle.check('10.0.0.1', T0 + 0.5), 1.5)
        self.assertEqual(throttle.check('10.0.0.1', T0 + 2), 0)
        self.assertEqual(throttle.check('10.0.0.9', T0), 0, 'global 5 // 3 = 1: no wait elsewhere')
        for _ in range(20):
            throttle.record_failure('10.0.0.1', now=T0 + 10)
        self.assertEqual(throttle.check('10.0.0.1', T0 + 10), 30)
        self.assertEqual(throttle.check('10.0.0.9', T0 + 10), 16, 'global 25 // 3 = 8 -> f(8) = 16 s')
        self.assertLess(real_time.monotonic() - started, 1.0, 'check never sleeps')

    def test_at_most_a_bounded_number_of_logins_wait_for_verification(self):
        throttle = auth.LoginThrottle(self.db)
        admitted = [throttle.try_enter() for _ in range(auth.LOGIN_PENDING_LIMIT)]
        self.assertTrue(all(admitted))
        self.assertFalse(throttle.try_enter(), 'the queue is full: answer at once instead of waiting')
        throttle.leave()
        self.assertTrue(throttle.try_enter())
        for _ in range(auth.LOGIN_PENDING_LIMIT):
            throttle.leave()
        with self.assertRaises(RuntimeError):
            throttle.leave()


KEY_PATTERN = r'wbk_[a-z2-7]{10}_[A-Za-z0-9_-]{43}'


class ApiKeyTests(DbCase):
    def setUp(self):
        super().setUp()
        self.keys = auth.ApiKeyStore(self.db)

    def refused(self, raw, now=T0 + 1) -> str:
        with self.assertRaises(auth.ApiKeyRefused) as caught:
            self.keys.verify(raw, now)
        return caught.exception.reason

    def test_issue_returns_the_raw_key_once_and_stores_only_the_secret_hash(self):
        import re
        issued = self.keys.issue('codex laptop', 'requester', {'all': True}, None, now=T0)
        self.assertRegex(issued.raw, f'^{KEY_PATTERN}$')
        key_id, secret = issued.raw[4:14], issued.raw[15:]
        self.assertEqual(issued.key_id, key_id)
        self.assertEqual(len(base64.urlsafe_b64decode(secret + '=')), 32)
        rows = self.rows('SELECT key_id, name, grade, scope_json, secret_hash, created_at, expires_at, revoked_at, '
                         'last_used_at, system FROM api_keys')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], (key_id, 'codex laptop', 'requester'))
        self.assertEqual(json.loads(rows[0][3]), {'all': True})
        self.assertEqual(rows[0][4], hashlib.sha256(secret.encode()).hexdigest())
        self.assertEqual(rows[0][5:], (T0, None, None, None, 0))
        blob = self.all_db_bytes()
        self.assertNotIn(secret.encode(), blob)
        self.assertNotIn(issued.raw.encode(), blob)
        self.assertFalse(re.search(KEY_PATTERN.encode(), blob))
        self.assertFalse(any('raw' in field for field in vars(type(self.keys.list()[0])).get('__slots__', ())))

        principal = self.keys.verify(issued.raw, T0 + 1)
        self.assertEqual((principal.key_id, principal.name, principal.grade, principal.scope, principal.system),
                         (key_id, 'codex laptop', 'requester', {'all': True}, False))

    def test_unknown_wrong_secret_and_malformed_keys_are_refused(self):
        issued = self.keys.issue('editor', 'editor', {'projects': ['p-1', 'p-2']}, None, now=T0)
        secret = issued.raw[15:]
        other_secret = ('A' if secret[0] != 'A' else 'B') + secret[1:]
        self.assertEqual(self.refused(issued.raw[:15] + other_secret), 'unknown')
        self.assertEqual(self.refused('wbk_aaaaaaaaaa_' + secret), 'unknown')
        for bad in ('', 'wbk_', issued.raw + 'x', issued.raw[:-1], 'wbx' + issued.raw[3:],
                    issued.raw[:14] + '-' + secret, 'wbk_AAAAAAAAAA_' + secret, 'wbk_aaaaaaaaa1_' + secret,
                    issued.raw[:15] + '!' + secret[1:], None, 42, b'wbk_'):
            with self.subTest(repr(bad)):
                self.assertEqual(self.refused(bad), 'malformed')

    def test_revoked_and_expired_keys_are_refused(self):
        revoked = self.keys.issue('old', 'editor', {'all': True}, None, now=T0)
        expiring = self.keys.issue('day pass', 'requester', {'projects': ['p-1']}, 1, now=T0)
        self.assertEqual(self.keys.verify(expiring.raw, T0 + DAY - 0.001).key_id, expiring.key_id)
        self.assertEqual(self.refused(expiring.raw, T0 + DAY), 'expired')
        info = self.keys.revoke(revoked.key_id, now=T0 + 10)
        self.assertEqual((info.key_id, info.revoked_at), (revoked.key_id, T0 + 10))
        self.assertEqual(self.refused(revoked.raw, T0 + 11), 'revoked')
        with self.assertRaises(auth.ApiKeyRejected) as caught:
            self.keys.revoke(revoked.key_id, now=T0 + 12)
        self.assertEqual(caught.exception.reason, 'already_revoked')
        with self.assertRaises(auth.ApiKeyRejected) as caught:
            self.keys.revoke('aaaaaaaaaa', now=T0 + 12)
        self.assertEqual(caught.exception.reason, 'not_found')

    def test_list_shows_name_scope_grade_last_use_and_expiry(self):
        a = self.keys.issue('alpha', 'requester', {'projects': ['p-1']}, 30, now=T0)
        b = self.keys.issue('beta', 'editor', {'all': True}, None, now=T0 + 1)
        listed = {info.key_id: info for info in self.keys.list()}
        self.assertEqual(set(listed), {a.key_id, b.key_id})
        alpha = listed[a.key_id]
        self.assertEqual((alpha.name, alpha.grade, alpha.scope, alpha.created_at, alpha.expires_at,
                          alpha.last_used_at, alpha.revoked_at, alpha.system),
                         ('alpha', 'requester', {'projects': ['p-1']}, T0, T0 + 30 * DAY, None, None, False))
        self.assertEqual(listed[b.key_id].expires_at, None)

    def test_issue_validates_name_grade_scope_and_expiry(self):
        self.keys.issue('taken', 'editor', {'all': True}, None, now=T0)
        cases = [
            (('', 'editor', {'all': True}, None), 'name_invalid'),
            (('x' * 65, 'editor', {'all': True}, None), 'name_invalid'),
            (('semi;colon', 'editor', {'all': True}, None), 'name_invalid'),
            (('taken', 'editor', {'all': True}, None), 'name_taken'),
            (('board-agent', 'editor', {'all': True}, None), 'name_reserved'),
            (('ok', 'owner', {'all': True}, None), 'grade_invalid'),
            (('ok', 'editor', {'all': False}, None), 'scope_invalid'),
            (('ok', 'editor', {'projects': []}, None), 'scope_invalid'),
            (('ok', 'editor', {'projects': ['p', 'p']}, None), 'scope_invalid'),
            (('ok', 'editor', {'projects': [1]}, None), 'scope_invalid'),
            (('ok', 'editor', {'all': True, 'projects': ['p']}, None), 'scope_invalid'),
            (('ok', 'editor', {'all': True}, 0), 'expiry_invalid'),
            (('ok', 'editor', {'all': True}, 3651), 'expiry_invalid'),
            (('ok', 'editor', {'all': True}, True), 'expiry_invalid'),
        ]
        for args, reason in cases:
            with self.subTest(args):
                with self.assertRaises(auth.ApiKeyRejected) as caught:
                    self.keys.issue(*args, now=T0)
                self.assertEqual(caught.exception.reason, reason)
        self.keys.issue('A-z 0.9_ok', 'requester', {'all': True}, 3650, now=T0)
        self.assertEqual(self.rows('SELECT count(*) FROM api_keys'), [(2,)])

    def test_a_thousand_consecutive_verifies_are_never_rate_limited(self):
        issued = self.keys.issue('busy agent', 'editor', {'all': True}, None, now=T0)
        for i in range(1000):
            self.assertEqual(self.keys.verify(issued.raw, T0 + i * 0.001).key_id, issued.key_id)

    def test_last_use_is_coalesced_in_memory_until_flush(self):
        issued = self.keys.issue('agent', 'editor', {'all': True}, None, now=T0)
        idle = self.keys.issue('idle', 'editor', {'all': True}, None, now=T0)
        for offset in (5, 9, 7):
            self.keys.verify(issued.raw, T0 + offset)
        self.assertEqual(self.rows('SELECT last_used_at FROM api_keys WHERE key_id = ?', issued.key_id), [(None,)],
                         'verify itself writes nothing')
        self.assertEqual(self.keys.flush(), 1)
        last = {info.key_id: info.last_used_at for info in self.keys.list()}
        self.assertEqual(last, {issued.key_id: T0 + 9, idle.key_id: None})
        self.assertEqual(self.keys.flush(), 0, 'nothing pending after a flush')
        self.keys.verify(issued.raw, T0 + 2)
        self.keys.flush()
        self.assertEqual(self.rows('SELECT last_used_at FROM api_keys WHERE key_id = ?', issued.key_id),
                         [(T0 + 9,)], 'a late-arriving older use never moves last use back')
        for bad in (issued.raw[:-1], 'wbk_aaaaaaaaaa_' + issued.raw[15:]):
            with self.assertRaises(auth.ApiKeyRefused):
                self.keys.verify(bad, T0 + 100)
        self.assertEqual(self.keys.flush(), 0, 'refused calls are not uses')


class SystemKeyTests(DbCase):
    def test_rotation_issues_an_editor_all_scope_key_and_refuses_the_previous_one(self):
        keys = auth.ApiKeyStore(self.db)
        user = keys.issue('mine', 'requester', {'all': True}, None, now=T0)
        first = keys.rotate_system_key(now=T0)
        self.assertRegex(first.raw, f'^{KEY_PATTERN}$')
        principal = keys.verify(first.raw, T0 + 1)
        self.assertEqual((principal.name, principal.grade, principal.scope, principal.system),
                         ('board-agent', 'editor', {'all': True}, True))
        second = keys.rotate_system_key(now=T0 + 100)
        with self.assertRaises(auth.ApiKeyRefused):
            keys.verify(first.raw, T0 + 101)
        self.assertTrue(keys.verify(second.raw, T0 + 101).system)
        system_rows = [info for info in keys.list() if info.system]
        self.assertEqual([(info.key_id, info.name, info.expires_at) for info in system_rows],
                         [(second.key_id, 'board-agent', None)])
        with self.assertRaises(auth.ApiKeyRejected) as caught:
            keys.revoke(second.key_id, now=T0 + 102)
        self.assertEqual(caught.exception.reason, 'system_key')
        self.assertTrue(keys.verify(second.raw, T0 + 103).system)
        self.assertEqual(keys.verify(user.raw, T0 + 103).key_id, user.key_id, 'user keys are untouched')
        self.assertNotIn(second.raw[15:].encode(), self.all_db_bytes())


class AuditTests(DbCase):
    def test_an_audit_row_has_the_spec_columns_and_only_allowed_detail(self):
        audit = auth.AuditLog(self.db)
        audit.record('login_failure', 'owner', '10.0.0.5', {'delay_seconds': 2}, now=T0)
        audit.record('api_key_issued', 'owner', '10.0.0.5',
                     {'key_id': 'abcdefghij', 'name': 'codex', 'grade': 'editor', 'scope': {'all': True},
                      'expires_at': None}, now=T0 + 1)
        audit.record('password_changed', 'cli', None, {'source': 'cli', 'invalidated_sessions': 3}, now=T0 + 2)
        rows = self.rows('SELECT id, at, event, actor, client_addr, detail_json FROM audit ORDER BY id')
        self.assertEqual([row[1:5] for row in rows],
                         [(T0, 'login_failure', 'owner', '10.0.0.5'), (T0 + 1, 'api_key_issued', 'owner', '10.0.0.5'),
                          (T0 + 2, 'password_changed', 'cli', None)])
        self.assertEqual(json.loads(rows[0][5]), {'delay_seconds': 2})
        self.assertEqual(json.loads(rows[2][5]), {'source': 'cli', 'invalidated_sessions': 3})
        page = audit.list_audit(1)
        self.assertEqual([entry.event for entry in page.entries], ['password_changed', 'api_key_issued', 'login_failure'])
        self.assertEqual((page.page, page.total), (1, 3))
        self.assertEqual(page.entries[1].detail['scope'], {'all': True})

    def test_unknown_events_and_input_bearing_detail_fields_are_refused(self):
        audit = auth.AuditLog(self.db)
        refused = [
            ('terminal_input', 'owner', 'a', {}),
            ('login_failure', 'owner', 'a', {'password': PASSWORD}),
            ('terminal_attach', 'owner', 'a', {'epoch': 1, 'data': 'ls -la\n'}),
            ('api_key_issued', 'owner', 'a', {'key_id': 'x', 'raw': 'wbk_x'}),
            ('web_control', 'owner', 'a', {'project_id': 'p', 'body': 'request text'}),
            ('board_start', 'owner', 'a', {'reason': 'x' * 10_000}),
            ('logout', 'owner', 'a', {'delay_seconds': object()}),
        ]
        for event, actor, addr, detail in refused:
            with self.subTest(event=event, detail=sorted(detail)):
                with self.assertRaises(auth.AuditRejected):
                    audit.record(event, actor, addr, detail, now=T0)
        self.assertEqual(self.rows('SELECT count(*) FROM audit'), [(0,)])
        for event in ('login_success', 'login_failure', 'logout', 'password_changed', 'api_key_issued',
                      'api_key_revoked', 'terminal_attach', 'terminal_takeover', 'terminal_release',
                      'terminal_timeout', 'terminal_closed', 'board_restart', 'board_start', 'board_stop',
                      'web_control'):
            audit.record(event, 'owner', None, {}, now=T0)
        self.assertEqual(self.rows('SELECT count(*) FROM audit'), [(15,)])

    def test_detail_values_are_checked_per_field_and_accept_the_spec_scope_shapes(self):
        audit = auth.AuditLog(self.db)
        key = {'key_id': 'abcdefghij', 'name': 'codex', 'grade': 'editor', 'expires_at': T0 + DAY}
        audit.record('api_key_issued', 'owner', 'a', dict(key, scope={'projects': ['p-1', 'p-2']}), now=T0)
        audit.record('api_key_revoked', 'owner', 'a', dict(key, scope={'all': True}, expires_at=None), now=T0)
        self.assertEqual(json.loads(self.rows('SELECT detail_json FROM audit ORDER BY id')[0][0])['scope'],
                         {'projects': ['p-1', 'p-2']})
        refused = [
            ('api_key_issued', dict(key, name=12345)),
            ('api_key_issued', dict(key, name='x' * 65)),
            ('api_key_issued', dict(key, grade='owner')),
            ('api_key_issued', dict(key, scope={'projects': []})),
            ('api_key_issued', dict(key, scope={'projects': [{'nested': 'x'}]})),
            ('api_key_issued', dict(key, scope={'all': True, 'note': 'x'})),
            ('api_key_issued', dict(key, expires_at='tomorrow')),
            ('password_changed', {'source': 'cli', 'invalidated_sessions': 'many'}),
            ('password_changed', {'source': 'somewhere'}),
            ('terminal_attach', {'epoch': 'one'}),
            ('login_failure', {'delay_seconds': True}),
            ('board_start', {'pid': '123'}),
            ('web_control', {'project_id': ['p']}),
        ]
        for event, detail in refused:
            with self.subTest(event=event, detail=detail):
                with self.assertRaises(auth.AuditRejected):
                    audit.record(event, 'owner', 'a', detail, now=T0)
        self.assertEqual(self.rows('SELECT count(*) FROM audit'), [(2,)])

    def test_a_new_password_generation_is_audited_once_even_across_a_hub_restart(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        for i in range(2):
            sessions.create(str(i), 'ua', T0, generation=self.generation)
        audit = auth.AuditLog(self.db)
        self.assertTrue(audit.note_password_generation(self.generation, 'cli', now=T0))
        self.assertFalse(audit.note_password_generation(self.generation, 'cli', now=T0 + 1), 'already audited')
        new = self.passwords.set_password('changed while the hub was down')  # 'wb hub passwd'
        self.db.close()
        reopened = auth.HubDb.open(self.state)
        self.addCleanup(reopened.close)
        later = auth.AuditLog(reopened)
        self.assertTrue(later.note_password_generation(new, 'cli', now=T0 + 2))
        self.assertFalse(later.note_password_generation(new, 'cli', now=T0 + 3))
        rows = self.rows("SELECT at, event, actor, detail_json FROM audit ORDER BY id")
        self.assertEqual([(at, event, actor, json.loads(detail)) for at, event, actor, detail in rows],
                         [(T0, 'password_changed', 'cli', {'source': 'cli', 'invalidated_sessions': 0}),
                          (T0 + 2, 'password_changed', 'cli', {'source': 'cli', 'invalidated_sessions': 2})])
        self.assertEqual(self.rows("SELECT failures FROM login_state WHERE key = 'meta:audited_generation'"), [(new,)])
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(0,)], 'stale sessions are dropped')

    def test_a_web_change_is_audited_with_its_own_source_and_count(self):
        audit = auth.AuditLog(self.db)
        audit.note_password_generation(self.generation, 'cli', now=T0)
        new = self.passwords.set_password('the new web password')
        self.assertTrue(audit.note_password_generation(new, 'web', invalidated=3, now=T0 + 1))
        self.assertFalse(audit.note_password_generation(new, 'cli', now=T0 + 2), 'the watchdog sees nothing new')
        last = self.rows("SELECT actor, detail_json FROM audit ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((last[0], json.loads(last[1])), ('owner', {'source': 'web', 'invalidated_sessions': 3}))

    def test_the_log_keeps_the_newest_50000_rows(self):
        import sqlite3
        connection = sqlite3.connect(self.db_path)
        with connection:
            connection.executemany('INSERT INTO audit (at, event, actor, client_addr, detail_json) '
                                   "VALUES (?, 'logout', 'owner', NULL, '{}')", [(T0 + i,) for i in range(50_000)])
        connection.close()
        audit = auth.AuditLog(self.db)
        audit.record('login_success', 'owner', '10.0.0.1', {'delay_seconds': 0}, now=T0 + 60_000)
        self.assertEqual(self.rows('SELECT count(*), min(at), max(at) FROM audit'), [(50_000, T0 + 1, T0 + 60_000)])
        page = audit.list_audit(1)
        self.assertEqual((page.total, page.entries[0].event), (50_000, 'login_success'))


class HardeningTests(DbCase):
    def test_unencodable_passwords_are_refused_not_raised(self):
        lone = '\ud800' * 12
        self.assertIsNone(self.passwords.verify(lone))
        with self.assertRaises(auth.PasswordRejected) as caught:
            self.passwords.set_password(lone)
        self.assertEqual(caught.exception.reason, 'password_invalid')
        self.assertEqual(self.passwords.verify(PASSWORD), self.generation, 'nothing was written')

    def test_creating_a_session_prunes_expired_ones(self):
        sessions = auth.SessionStore(self.db, self.passwords)
        old = sessions.create('a', 'ua', T0, generation=self.generation)
        live = sessions.create('b', 'ua', T0 + DAY - 10, generation=self.generation)
        sessions.create('c', 'ua', T0 + DAY, generation=self.generation)
        self.assertEqual(self.rows('SELECT count(*) FROM sessions'), [(2,)])
        self.assertIsNone(sessions.verify(old.token, T0 + DAY))
        self.assertIsNotNone(sessions.verify(live.token, T0 + DAY))

    def test_idle_address_rows_are_pruned_but_the_global_count_is_kept(self):
        throttle = auth.LoginThrottle(self.db)
        for _ in range(5):
            throttle.record_failure('10.0.0.1', now=T0)
        throttle.record_failure('10.0.0.2', now=T0 + auth.LOGIN_STATE_TTL + 1)
        keys = {key for (key,) in self.rows('SELECT key FROM login_state')}
        self.assertEqual(keys, {'addr:10.0.0.2', 'global'})
        self.assertEqual(self.rows("SELECT failures FROM login_state WHERE key = 'global'"), [(6,)])

    def test_list_audit_clamps_the_page(self):
        audit = auth.AuditLog(self.db)
        for i in range(250):
            audit.record('logout', 'owner', None, {}, now=T0 + i)
        self.assertEqual((audit.list_audit(3).page, len(audit.list_audit(3).entries)), (3, 50))
        beyond = audit.list_audit(10 ** 12)
        self.assertEqual((beyond.page, beyond.entries, beyond.total), (4, [], 250), 'no huge OFFSET')
        for bad in (0, -5, 'x', None, 2.5, True):
            with self.subTest(bad=bad):
                page = audit.list_audit(bad)
                self.assertEqual(page.page, 2 if bad == 2.5 else 1)
        self.assertEqual(len(audit.list_audit(1, page_size=10 ** 9).entries), 250)
        self.assertEqual(len(audit.list_audit(1, page_size=0).entries), 1)
        empty = auth.AuditLog(auth.HubDb.open(self.base_empty()))
        self.assertEqual((empty.list_audit(7).page, empty.list_audit(7).entries), (1, []))
        self.assertEqual(empty.list_audit(10 ** 30).entries, [])
        empty.db.close()

    def base_empty(self) -> Path:
        return Path(self.temp.name).resolve() / 'other-state'


if __name__ == '__main__':
    unittest.main()
