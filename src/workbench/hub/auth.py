"""Hub authentication core (Task Inbox SPEC "인증·세션·CSRF", "감사 기록", "API key").

No HTTP here. ``auth.json`` (+ ``auth.lock``) holds the scrypt password record; ``wb hub passwd``
and the Hub both write it under the lock. ``hub.sqlite3`` holds sessions, API keys, login
failure counts and the audit log; only the Hub opens it (single writer).

Secrets never reach a file, log or audit row: session tokens and API key secrets are kept as
SHA-256 digests only, every secret comparison is ``hmac.compare_digest``.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time

from workbench.backend.paths import ensure_private_dir, write_private_bytes

AUTH_NAME = "auth.json"
AUTH_LOCK_NAME = "auth.lock"
HUB_DB_NAME = "hub.sqlite3"

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 32768, 8, 1, 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
PASSWORD_MIN, PASSWORD_MAX = 10, 1024
AUTH_FILE_LIMIT = 64 * 1024
LOCK_TIMEOUT = 10.0


class AuthInvalid(RuntimeError):
    """``auth.json`` exists but is malformed or unsafe: never read as "no password"."""

    reason = "auth_invalid"


class PasswordRejected(ValueError):
    """The new password breaks the length rule; nothing was written."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def password_problem(password: str) -> str | None:
    """``password_too_short``/``password_too_long`` or ``None`` (SPEC: 10..1024 characters)."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        return "password_too_short"
    if len(password) > PASSWORD_MAX:
        return "password_too_long"
    if not _encodable(password):
        return "password_invalid"
    return None


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates
        return False
    return True


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                          dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)


@dataclass(frozen=True, slots=True)
class PasswordRecord:
    salt: bytes
    digest: bytes
    generation: int
    changed_at: float


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: object, size: int) -> bytes:
    if not isinstance(text, str):
        raise ValueError("not text")
    data = base64.b64decode(text.encode("ascii"), validate=True)
    if len(data) != size:
        raise ValueError("wrong size")
    return data


_RECORD_KEYS = frozenset({"schema_version", "kdf", "n", "r", "p", "dklen", "salt", "hash", "generation",
                          "changed_at"})


def _parse_record(value: object) -> PasswordRecord:
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise ValueError("unexpected keys")
    expected = {"schema_version": 1, "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                "dklen": SCRYPT_DKLEN}
    for key, wanted in expected.items():
        if type(value[key]) is not type(wanted) or value[key] != wanted:
            raise ValueError(f"unexpected {key}")
    generation, changed_at = value["generation"], value["changed_at"]
    if type(generation) is not int or generation < 1:
        raise ValueError("bad generation")
    if type(changed_at) not in (int, float):
        raise ValueError("bad changed_at")
    return PasswordRecord(_unb64(value["salt"], SALT_BYTES), _unb64(value["hash"], SCRYPT_DKLEN),
                          generation, float(changed_at))


class _FileLock:
    """Blocking (bounded) exclusive flock on a private regular lock file; refuses symlinks."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT):
        self.path, self.timeout, self._fd = path, timeout, None

    def __enter__(self) -> "_FileLock":
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise AuthInvalid(f"unsafe lock file: {self.path}")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except BaseException:
            os.close(descriptor)
            raise
        self._fd = descriptor
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class PasswordStore:
    """``<hub state>/auth.json``: read, verify and (under ``auth.lock``) replace atomically."""

    def __init__(self, state_dir: Path, *, clock=time.time):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / AUTH_NAME
        self.lock_path = self.state_dir / AUTH_LOCK_NAME
        self._clock = clock

    def load(self) -> PasswordRecord | None:
        """``None`` only when no file exists; anything unreadable raises :class:`AuthInvalid`."""
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuthInvalid(f"cannot open {self.path}: {exc.strerror}") from exc
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > AUTH_FILE_LIMIT):
                raise AuthInvalid(f"unsafe password file: {self.path}")
            raw = os.read(descriptor, AUTH_FILE_LIMIT + 1)
        finally:
            os.close(descriptor)
        try:
            return _parse_record(json.loads(raw))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuthInvalid(f"malformed password file: {self.path}") from exc

    def generation(self) -> int | None:
        record = self.load()
        return None if record is None else record.generation

    def verify(self, password: str) -> int | None:
        """The generation the password matched (always >= 1), else ``None``.

        One file read gives both the hash and the generation, so a caller that binds a new
        session to the returned generation never mixes an old password with a new generation.
        """
        record = self.load()
        if (record is None or not isinstance(password, str) or len(password) > PASSWORD_MAX
                or not _encodable(password)):
            return None
        if hmac.compare_digest(_scrypt(password, record.salt), record.digest):
            return record.generation
        return None

    def set_password(self, password: str) -> int:
        """Write a new record with ``generation + 1``; returns the new generation.

        Without a readable previous record the first generation is random (not 1), so a
        session row left from a deleted or corrupt ``auth.json`` cannot match it again.
        """
        problem = password_problem(password)
        if problem is not None:
            raise PasswordRejected(problem)
        salt = secrets.token_bytes(SALT_BYTES)
        digest = _scrypt(password, salt)
        ensure_private_dir(self.state_dir)
        with _FileLock(self.lock_path):
            try:
                previous = self.load()
            except AuthInvalid:
                previous = None
            generation = previous.generation + 1 if previous is not None else secrets.randbelow(1 << 48) + 1
            record = {"schema_version": 1, "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                      "dklen": SCRYPT_DKLEN, "salt": _b64(salt), "hash": _b64(digest),
                      "generation": generation, "changed_at": float(self._clock())}
            write_private_bytes(self.path, json.dumps(record, sort_keys=True).encode("ascii"))
        return generation


# --- hub.sqlite3 ------------------------------------------------------------------------------

SCHEMA_VERSION = 1
SESSION_TTL = 24 * 3600.0
TOKEN_BYTES = 32
TOKEN_TEXT_LIMIT = 256

_SCHEMA = """
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
    password_generation INTEGER NOT NULL, csrf_token TEXT NOT NULL, client_addr TEXT, user_agent TEXT);
CREATE TABLE api_keys (
    key_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, grade TEXT NOT NULL, scope_json TEXT NOT NULL,
    secret_hash TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL, revoked_at REAL,
    last_used_at REAL, system INTEGER NOT NULL DEFAULT 0);
CREATE TABLE login_state (key TEXT PRIMARY KEY, failures INTEGER NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL,
    client_addr TEXT, detail_json TEXT NOT NULL);
"""
# ``login_state`` also holds the one meta row ``meta:audited_generation`` (``failures`` = the last
# audited password generation): the SPEC fixes the four tables, and login keys are ``addr:``/``global``.
AUDITED_GENERATION_KEY = "meta:audited_generation"


class HubDbError(RuntimeError):
    """``hub.sqlite3`` is unsafe (mode, owner, symlink) or has an unknown schema."""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")


class HubDb:
    """One serialized connection to ``<hub state>/hub.sqlite3`` (0600, WAL, ``synchronous=FULL``).

    Only the Hub opens it. Methods may be called from executor threads; a lock serializes them.
    """

    def __init__(self, path: Path, connection):
        self.path, self._connection, self._lock = path, connection, threading.RLock()

    @classmethod
    def open(cls, state_dir: Path) -> "HubDb":
        state_dir = ensure_private_dir(Path(state_dir))
        path = state_dir / HUB_DB_NAME
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        except FileExistsError:
            pass
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise HubDbError(f"cannot stat {path}: {exc.strerror}") from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise HubDbError(f"unsafe Hub DB file: {path}")
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                tables = connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
                if tables:
                    raise HubDbError(f"Hub DB without a schema version: {path}")
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.execute("COMMIT")
            elif version != SCHEMA_VERSION:
                raise HubDbError(f"unsupported Hub DB schema {version}: {path}")
        except BaseException:
            connection.close()
            raise
        return cls(path, connection)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._connection.execute(sql, args).fetchall()

    def write(self, steps) -> object:
        """Run ``steps(execute)`` in one ``BEGIN IMMEDIATE`` transaction; returns its result."""
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = steps(lambda sql, args=(): connection.execute(sql, args))
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            return result


# --- sessions ---------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class IssuedSession:
    """The raw token exists only here (it goes into the cookie); the DB keeps its SHA-256."""

    token: str
    csrf: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class Session:
    token_hash: str
    created_at: float
    expires_at: float
    password_generation: int
    csrf: str
    client_addr: str | None
    user_agent: str | None


class SessionStore:
    """Login sessions: 24 h absolute expiry, valid only for the password generation they were bound to."""

    def __init__(self, db: HubDb, passwords: PasswordStore):
        self.db, self.passwords = db, passwords

    def create(self, client_addr: str | None, user_agent: str | None, now: float, *, generation: int) -> IssuedSession:
        """``generation`` is the value :meth:`PasswordStore.verify` returned for this login."""
        token, csrf = _token(), _token()
        expires_at = now + SESSION_TTL

        def steps(execute):
            execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))  # expired logins never verify again
            execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_token_hash(token), now, expires_at, generation, csrf, client_addr, user_agent))
        self.db.write(steps)
        return IssuedSession(token, csrf, expires_at)

    def _row(self, token: object) -> Session | None:
        if not isinstance(token, str) or not token or len(token) > TOKEN_TEXT_LIMIT:
            return None
        rows = self.db.query("SELECT token_hash, created_at, expires_at, password_generation, csrf_token, "
                             "client_addr, user_agent FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        return Session(*rows[0]) if rows else None

    def verify(self, token: str, now: float) -> Session | None:
        """The session when unexpired (``now < expires_at``) and bound to the current generation."""
        session = self._row(token)
        if session is None or not now < session.expires_at:
            return None
        try:
            current = self.passwords.generation()
        except AuthInvalid:
            return None
        if current is None or current != session.password_generation:
            return None
        return session

    def delete(self, token: str) -> None:
        if isinstance(token, str) and len(token) <= TOKEN_TEXT_LIMIT:
            self.db.write(lambda execute: execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),)))

    def delete_all_except(self, token: str) -> int:
        """Delete every other session; returns how many were deleted."""
        keep = _token_hash(token) if isinstance(token, str) else ""
        return self.db.write(lambda execute: execute(
            "DELETE FROM sessions WHERE token_hash != ?", (keep,)).rowcount)

    def reissue(self, token: str, now: float, *, expected_generation: int | None = None) -> "Reissued | None":
        """After a web password change: replace this unexpired login with one bound to the
        current generation (same expiry, new token and CSRF) and delete every other session.

        Web flow: ``g = passwords.verify(current)``; ``passwords.set_password(new)``;
        ``reissue(token, now, expected_generation=g)``. The session must carry exactly
        ``expected_generation`` and the current generation must be exactly the one change after
        it (``expected + 1``), so a session an earlier change invalidated is never revived and a
        change slipped in between (e.g. ``wb hub passwd``) refuses the reissue. ``None`` means
        "the generation just before the current one". The generation is read while holding
        ``auth.lock`` inside the DB transaction, so no password write can interleave.
        """
        if self._row(token) is None:
            return None
        fresh_token, fresh_csrf = _token(), _token()

        def steps(execute):
            try:
                generation = self.passwords.generation()
            except AuthInvalid:
                return None
            rows = execute("SELECT token_hash, created_at, expires_at, password_generation, csrf_token, "
                           "client_addr, user_agent FROM sessions WHERE token_hash = ?",
                           (_token_hash(token),)).fetchall()
            if generation is None or not rows:
                return None
            old = Session(*rows[0])
            expected = generation - 1 if expected_generation is None else expected_generation
            if (not now < old.expires_at or old.password_generation != expected
                    or type(expected) is not int or generation != expected + 1):
                return None
            execute("DELETE FROM sessions WHERE token_hash = ?", (old.token_hash,))
            invalidated = execute("DELETE FROM sessions").rowcount
            execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_token_hash(fresh_token), old.created_at, old.expires_at, generation, fresh_csrf,
                     old.client_addr, old.user_agent))
            return Reissued(IssuedSession(fresh_token, fresh_csrf, old.expires_at), invalidated)
        try:
            with _FileLock(self.passwords.lock_path):
                return self.db.write(steps)
        except (AuthInvalid, OSError):
            return None


@dataclass(frozen=True, slots=True)
class Reissued:
    session: IssuedSession
    invalidated: int  # other sessions deleted (the audit ``password_changed`` detail)


# --- login failure delay ----------------------------------------------------------------------

DELAY_CAP = 30
GLOBAL_KEY = "global"
LOGIN_PENDING_LIMIT = 8
LOGIN_STATE_TTL = 24 * 3600.0
TRUSTED_TTL = 30 * 24 * 3600.0  # an address that logged in this recently skips the global delay


def _loopback(addr: str | None) -> bool:
    try:
        address = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def delay_seconds(failures: int) -> int:
    """SPEC ``f(n)``: 0 for n <= 3, else ``min(2^(n-4), 30)`` seconds."""
    return 0 if failures <= 3 else min(2 ** min(failures - 4, 5), DELAY_CAP)


class LoginThrottle:
    """Consecutive login failures per client address and Hub-wide (``login_state``, kept across restarts).

    The wait before the next verification is ``max(f(addr), f(global // 3))`` counted from the
    last failure of that counter. Intended use by the web login handler (TI-03), so that a LAN
    attacker can never make the owner wait behind a sleeping lock holder:

    1. ``if not throttle.try_enter():`` answer 429 at once (at most :data:`LOGIN_PENDING_LIMIT`
       logins may be in flight); otherwise ``try: ... finally: throttle.leave()``.
    2. ``retry_after = throttle.check(addr, now)``; when ``> 0`` answer 429 with
       ``Retry-After: ceil(retry_after)`` without verifying and without sleeping.
    3. Verify the password (in an executor), then ``record_failure``/``record_success``.

    Address rows idle for :data:`LOGIN_STATE_TTL` are pruned on each failure.

    Owner lockout guard (orchestrator decision 2026-10-25): the global term does not apply to a
    loopback address or to an address with a successful login in the last :data:`TRUSTED_TTL`
    (``ok:<addr>`` rows, ``updated_at`` = the success time). Their own per-address delay still does.
    """

    def __init__(self, db: HubDb, *, clock=time.time, pending_limit: int | None = None):
        self.db, self._clock = db, clock
        self._pending_limit = LOGIN_PENDING_LIMIT if pending_limit is None else pending_limit
        self._pending = 0
        self._pending_lock = threading.Lock()

    def try_enter(self) -> bool:
        """Take one of the bounded in-flight login slots; ``False`` when all are taken."""
        with self._pending_lock:
            if self._pending >= self._pending_limit:
                return False
            self._pending += 1
            return True

    def leave(self) -> None:
        with self._pending_lock:
            if self._pending <= 0:
                raise RuntimeError("leave() without try_enter()")
            self._pending -= 1

    @staticmethod
    def _key(addr: str | None) -> str:
        return f"addr:{addr or ''}"

    @staticmethod
    def _trusted_key(addr: str | None) -> str:
        return f"ok:{addr or ''}"

    def global_exempt(self, addr: str | None, now: float) -> bool:
        """Loopback, or a successful login from ``addr`` within :data:`TRUSTED_TTL` before ``now``."""
        if _loopback(addr):
            return True
        _count, succeeded_at = self._state(self._trusted_key(addr))
        return succeeded_at > 0 and now - succeeded_at <= TRUSTED_TTL

    def _state(self, key: str) -> tuple[int, float]:
        rows = self.db.query("SELECT failures, updated_at FROM login_state WHERE key = ?", (key,))
        return (rows[0][0], rows[0][1]) if rows else (0, 0.0)

    def check(self, addr: str | None, now: float) -> float:
        """Seconds the next attempt from ``addr`` must still wait (0 = verify now). Never sleeps."""
        own_failures, own_at = self._state(self._key(addr))
        own_wait = own_at + delay_seconds(own_failures) - now
        if self.global_exempt(addr, now):
            return max(0.0, own_wait)
        all_failures, all_at = self._state(GLOBAL_KEY)
        return max(0.0, own_wait, all_at + delay_seconds(all_failures // 3) - now)

    def _failures(self, key: str) -> int:
        rows = self.db.query("SELECT failures FROM login_state WHERE key = ?", (key,))
        return rows[0][0] if rows else 0

    def delay_for(self, addr: str | None, now: float | None = None) -> int:
        now = float(self._clock() if now is None else now)
        own = delay_seconds(self._failures(self._key(addr)))
        if self.global_exempt(addr, now):
            return own
        return max(own, delay_seconds(self._failures(GLOBAL_KEY) // 3))

    def record_failure(self, addr: str | None, now: float | None = None) -> None:
        now = float(self._clock() if now is None else now)

        def steps(execute):
            execute("DELETE FROM login_state WHERE key LIKE 'addr:%' AND updated_at < ?", (now - LOGIN_STATE_TTL,))
            execute("DELETE FROM login_state WHERE key LIKE 'ok:%' AND updated_at < ?", (now - TRUSTED_TTL,))
            for key in (self._key(addr), GLOBAL_KEY):
                execute("INSERT INTO login_state VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE "
                        "SET failures = failures + 1, updated_at = excluded.updated_at", (key, now))
        self.db.write(steps)

    def record_success(self, addr: str | None, now: float | None = None) -> None:
        """Clear that address and the global count; remember ``addr`` as a recent owner address."""
        now = float(self._clock() if now is None else now)

        def steps(execute):
            execute("DELETE FROM login_state WHERE key IN (?, ?)", (self._key(addr), GLOBAL_KEY))
            execute("INSERT INTO login_state VALUES (?, 0, ?) ON CONFLICT(key) DO UPDATE "
                    "SET updated_at = excluded.updated_at", (self._trusted_key(addr), now))
        self.db.write(steps)


# --- API keys ---------------------------------------------------------------------------------

KEY_PREFIX = "wbk_"
KEY_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # RFC 4648 base32, lower case
KEY_ID_LENGTH, KEY_SECRET_LENGTH = 10, 43
KEY_LENGTH = len(KEY_PREFIX) + KEY_ID_LENGTH + 1 + KEY_SECRET_LENGTH
KEY_ID_ATTEMPTS = 8
_BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
GRADES = ("requester", "editor")
SYSTEM_KEY_NAME = "board-agent"
KEY_NAME_MAX = 64
_KEY_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._-")
KEY_EXPIRY_DAYS_MAX = 3650
PROJECT_ID_MAX = 128
SCOPE_PROJECTS_MAX = 1000
_DUMMY_HASH = hashlib.sha256(b"").hexdigest()


class ApiKeyRefused(Exception):
    """``verify`` refusal (HTTP 401): ``malformed``, ``unknown``, ``revoked`` or ``expired``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ApiKeyRejected(ValueError):
    """``issue``/``revoke`` input refusal; nothing was written."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IssuedKey:
    raw: str  # shown once on the issue result page; stored nowhere
    key_id: str


@dataclass(frozen=True, slots=True)
class ApiKeyPrincipal:
    key_id: str
    name: str
    grade: str
    scope: dict
    system: bool


@dataclass(frozen=True, slots=True)
class ApiKeyInfo:
    key_id: str
    name: str
    grade: str
    scope: dict
    created_at: float
    expires_at: float | None
    revoked_at: float | None
    last_used_at: float | None
    system: bool


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _split_key(raw: object) -> tuple[str, str] | None:
    if not isinstance(raw, str) or len(raw) != KEY_LENGTH or not raw.startswith(KEY_PREFIX):
        return None
    key_id, separator, secret = raw[4:14], raw[14], raw[15:]
    if separator != "_" or any(char not in KEY_ID_ALPHABET for char in key_id):
        return None
    if any(char not in _BASE64URL for char in secret):
        return None
    decoded = base64.urlsafe_b64decode(secret + "=")
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != secret:
        return None
    return key_id, secret


def _check_scope(scope: object) -> dict:
    if isinstance(scope, dict) and set(scope) == {"all"} and scope["all"] is True:
        return {"all": True}
    if isinstance(scope, dict) and set(scope) == {"projects"} and isinstance(scope["projects"], list):
        projects = scope["projects"]
        if (0 < len(projects) <= SCOPE_PROJECTS_MAX
                and all(isinstance(item, str) and 0 < len(item) <= PROJECT_ID_MAX for item in projects)
                and len(set(projects)) == len(projects)):
            return {"projects": list(projects)}
    raise ApiKeyRejected("scope_invalid")


_KEY_COLUMNS = "key_id, name, grade, scope_json, created_at, expires_at, revoked_at, last_used_at, system"


def _info(row: tuple) -> ApiKeyInfo:
    key_id, name, grade, scope_json, created_at, expires_at, revoked_at, last_used_at, system = row
    return ApiKeyInfo(key_id, name, grade, json.loads(scope_json), created_at, expires_at, revoked_at,
                      last_used_at, bool(system))


class ApiKeyStore:
    """``hub.sqlite3.api_keys``: ``wbk_<key_id>_<secret>`` keys stored as SHA-256(secret) only."""

    def __init__(self, db: HubDb):
        self.db = db
        self._last_used: dict[str, float] = {}  # key_id -> latest use not yet written
        self._pending_lock = threading.Lock()

    def flush(self) -> int:
        """Write coalesced last-use times (SPEC: every 10 s and at Hub stop); returns keys written."""
        with self._pending_lock:
            pending, self._last_used = self._last_used, {}
        if not pending:
            return 0

        def steps(execute):
            for key_id, used_at in pending.items():
                execute("UPDATE api_keys SET last_used_at = max(coalesce(last_used_at, ?), ?) WHERE key_id = ?",
                        (used_at, used_at, key_id))
        try:
            self.db.write(steps)
        except BaseException:
            with self._pending_lock:  # keep them for the next flush
                for key_id, used_at in pending.items():
                    self._last_used[key_id] = max(used_at, self._last_used.get(key_id, used_at))
            raise
        return len(pending)

    def _insert(self, name: str, grade: str, scope: dict, expires_at: float | None, now: float,
                system: bool, replace_system: bool = False) -> IssuedKey:
        for _ in range(KEY_ID_ATTEMPTS):
            key_id = "".join(secrets.choice(KEY_ID_ALPHABET) for _ in range(KEY_ID_LENGTH))
            secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")

            def steps(execute):
                if replace_system:
                    execute("DELETE FROM api_keys WHERE system = 1")
                execute("INSERT INTO api_keys (key_id, name, grade, scope_json, secret_hash, created_at, "
                        "expires_at, system) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (key_id, name, grade, json.dumps(scope, sort_keys=True), _secret_hash(secret), now,
                         expires_at, int(system)))
            try:
                self.db.write(steps)
            except sqlite3.IntegrityError:
                if self.db.query("SELECT 1 FROM api_keys WHERE name = ?", (name,)) and not replace_system:
                    raise ApiKeyRejected("name_taken") from None
                continue  # key_id collision: draw again
            return IssuedKey(f"{KEY_PREFIX}{key_id}_{secret}", key_id)
        raise HubDbError("could not draw an unused API key id")

    def issue(self, name: str, grade: str, scope: dict, expires_days: int | None, *, now: float) -> IssuedKey:
        """Owner-issued key; ``scope`` project ids must already be checked against the registry."""
        if (not isinstance(name, str) or not 0 < len(name) <= KEY_NAME_MAX
                or any(char not in _KEY_NAME_CHARS for char in name)):
            raise ApiKeyRejected("name_invalid")
        if name == SYSTEM_KEY_NAME:
            raise ApiKeyRejected("name_reserved")
        if grade not in GRADES:
            raise ApiKeyRejected("grade_invalid")
        scope = _check_scope(scope)
        if expires_days is not None and (type(expires_days) is not int
                                         or not 1 <= expires_days <= KEY_EXPIRY_DAYS_MAX):
            raise ApiKeyRejected("expiry_invalid")
        expires_at = None if expires_days is None else now + expires_days * 86400.0
        return self._insert(name, grade, scope, expires_at, now, False)

    def rotate_system_key(self, *, now: float) -> IssuedKey:
        """Board agent start: a new editor, all-scope, non-expiring ``board-agent`` key.

        The previous system key row is deleted in the same transaction (the name is UNIQUE),
        so its raw key verifies as ``unknown`` from then on.
        """
        return self._insert(SYSTEM_KEY_NAME, "editor", {"all": True}, None, now, True, replace_system=True)

    def verify(self, raw: str, now: float) -> ApiKeyPrincipal:
        """The key's principal, or :class:`ApiKeyRefused`. No rate limit."""
        parts = _split_key(raw)
        if parts is None:
            raise ApiKeyRefused("malformed")
        key_id, secret = parts
        rows = self.db.query("SELECT name, grade, scope_json, secret_hash, expires_at, revoked_at, system "
                             "FROM api_keys WHERE key_id = ?", (key_id,))
        stored = rows[0][3] if rows else _DUMMY_HASH
        matched = hmac.compare_digest(_secret_hash(secret), stored)
        if not rows or not matched:
            raise ApiKeyRefused("unknown")
        name, grade, scope_json, _, expires_at, revoked_at, system = rows[0]
        if revoked_at is not None:
            raise ApiKeyRefused("revoked")
        if expires_at is not None and not now < expires_at:
            raise ApiKeyRefused("expired")
        with self._pending_lock:
            self._last_used[key_id] = max(now, self._last_used.get(key_id, now))
        return ApiKeyPrincipal(key_id, name, grade, json.loads(scope_json), bool(system))

    def revoke(self, key_id: str, *, now: float) -> ApiKeyInfo:
        """Owner revocation of a listed key; the system key is not revocable here."""
        def steps(execute):
            rows = execute(f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE key_id = ?", (key_id,)).fetchall()
            if not rows:
                raise ApiKeyRejected("not_found")
            info = _info(rows[0])
            if info.system:
                raise ApiKeyRejected("system_key")
            if info.revoked_at is not None:
                raise ApiKeyRejected("already_revoked")
            execute("UPDATE api_keys SET revoked_at = ? WHERE key_id = ?", (now, key_id))
            return _info(rows[0][:6] + (now,) + rows[0][7:])
        return self.db.write(steps)

    def list(self) -> list[ApiKeyInfo]:
        """Every key (revoked ones included, ``revoked_at`` set), the system key first."""
        return [_info(row) for row in self.db.query(
            f"SELECT {_KEY_COLUMNS} FROM api_keys ORDER BY system DESC, created_at, key_id")]


# --- audit ------------------------------------------------------------------------------------

AUDIT_LIMIT = 50_000
AUDIT_PAGE_SIZE = 100
AUDIT_PAGE_SIZE_MAX = 1000
AUDIT_TEXT_LIMIT = 256
_KEY_DETAIL = frozenset({"key_id", "name", "grade", "scope", "expires_at"})
_TERMINAL_DETAIL = frozenset({"epoch", "previous_epoch"})
_BOARD_DETAIL = frozenset({"channel", "pid", "reason"})
# SPEC "감사 기록": the only events and detail fields; no input, password, secret or body field exists.
AUDIT_EVENTS: dict[str, frozenset] = {
    "login_success": frozenset({"delay_seconds"}),
    "login_failure": frozenset({"delay_seconds"}),
    "logout": frozenset({"delay_seconds"}),
    "password_changed": frozenset({"source", "invalidated_sessions"}),
    "api_key_issued": _KEY_DETAIL,
    "api_key_revoked": _KEY_DETAIL,
    "terminal_attach": _TERMINAL_DETAIL,
    "terminal_takeover": _TERMINAL_DETAIL,
    "terminal_release": _TERMINAL_DETAIL,
    "terminal_timeout": _TERMINAL_DETAIL,
    "terminal_closed": _TERMINAL_DETAIL,
    "board_restart": _BOARD_DETAIL,
    "board_start": _BOARD_DETAIL,
    "board_stop": _BOARD_DETAIL,
    "web_control": frozenset({"project_id", "action", "result"}),
}


class AuditRejected(ValueError):
    """Unknown event, a detail field outside the SPEC list, or an oversized/unsupported value."""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    at: float
    event: str
    actor: str
    client_addr: str | None
    detail: dict


@dataclass(frozen=True, slots=True)
class AuditPage:
    entries: list
    page: int
    total: int


def _number(value: object) -> bool:
    return type(value) in (int, float) and value == value and abs(value) != float("inf")


def _text(limit: int):
    return lambda value: isinstance(value, str) and len(value) <= limit


def _scope_ok(value: object) -> bool:
    try:
        return _check_scope(value) == value
    except ApiKeyRejected:
        return False


# One check per SPEC detail field: the value type is fixed, not only the key name.
_DETAIL_CHECKS = {
    "delay_seconds": lambda value: _number(value) and value >= 0,
    "source": lambda value: value in ("cli", "web"),
    "invalidated_sessions": lambda value: type(value) is int and value >= 0,
    "key_id": _text(KEY_ID_LENGTH),
    "name": _text(KEY_NAME_MAX),
    "grade": lambda value: value in GRADES,
    "scope": _scope_ok,
    "expires_at": lambda value: value is None or _number(value),
    "epoch": lambda value: value is None or type(value) is int,
    "previous_epoch": lambda value: value is None or type(value) is int,
    "channel": _text(32),
    "pid": lambda value: value is None or type(value) is int,
    "reason": _text(AUDIT_TEXT_LIMIT),
    "project_id": _text(PROJECT_ID_MAX),
    "action": _text(64),
    "result": _text(AUDIT_TEXT_LIMIT),
}


class AuditLog:
    """``hub.sqlite3.audit``: the newest :data:`AUDIT_LIMIT` rows are kept."""

    def __init__(self, db: HubDb, *, limit: int = AUDIT_LIMIT):
        self.db, self.limit = db, limit

    @staticmethod
    def _payload(event: str, actor: str, client_addr: str | None, detail: dict) -> str:
        allowed = AUDIT_EVENTS.get(event)
        if allowed is None:
            raise AuditRejected(f"unknown audit event {event!r}")
        if not isinstance(detail, dict) or not set(detail) <= allowed:
            raise AuditRejected(f"detail fields not allowed for {event}")
        for field, value in detail.items():
            if not _DETAIL_CHECKS[field](value):
                raise AuditRejected(f"detail field {field} has an unsupported value")
        for text in (actor, client_addr):
            if text is not None and (not isinstance(text, str) or len(text) > AUDIT_TEXT_LIMIT):
                raise AuditRejected("actor/client_addr must be short text")
        return json.dumps(detail, sort_keys=True, ensure_ascii=False)

    def _insert(self, execute, now: float, event: str, actor: str, client_addr: str | None, payload: str) -> None:
        execute("INSERT INTO audit (at, event, actor, client_addr, detail_json) VALUES (?, ?, ?, ?, ?)",
                (now, event, actor, client_addr, payload))
        execute("DELETE FROM audit WHERE id IN (SELECT id FROM audit ORDER BY id DESC LIMIT -1 OFFSET ?)",
                (self.limit,))

    def record(self, event: str, actor: str, client_addr: str | None, detail: dict, *, now: float) -> None:
        payload = self._payload(event, actor, client_addr, detail)
        self.db.write(lambda execute: self._insert(execute, now, event, actor, client_addr, payload))

    def note_password_generation(self, generation: int, source: str, *, now: float,
                                 invalidated: int | None = None, client_addr: str | None = None) -> bool:
        """Record ``password_changed{source}`` once per password generation; ``True`` when recorded.

        The last audited generation is kept in the ``login_state`` row ``meta:audited_generation``,
        so a ``wb hub passwd``
        made while the Hub was down is audited when the Hub next starts (or its watchdog sees
        the new ``auth.json``). Sessions bound to another generation are deleted (they can never
        verify again) and counted, unless the caller passes its own count (the web change,
        after :meth:`SessionStore.reissue`). Any generation other than the audited one counts
        as new: a recreated ``auth.json`` starts at a random generation.
        """
        if type(generation) is not int or source not in ("cli", "web"):
            raise AuditRejected("generation must be an int and source cli|web")
        actor = "cli" if source == "cli" else "owner"

        def steps(execute):
            rows = execute("SELECT failures FROM login_state WHERE key = ?", (AUDITED_GENERATION_KEY,)).fetchall()
            if rows and rows[0][0] == generation:
                return False
            dropped = execute("DELETE FROM sessions WHERE password_generation != ?", (generation,)).rowcount
            count = dropped if invalidated is None else invalidated
            payload = self._payload("password_changed", actor, client_addr,
                                    {"source": source, "invalidated_sessions": count})
            self._insert(execute, now, "password_changed", actor, client_addr, payload)
            execute("INSERT INTO login_state VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                    "SET failures = excluded.failures, updated_at = excluded.updated_at",
                    (AUDITED_GENERATION_KEY, generation, now))
            return True
        return self.db.write(steps)

    def list_audit(self, page: object = 1, *, page_size: int = AUDIT_PAGE_SIZE) -> AuditPage:
        """Newest first; ``page`` starts at 1 (junk means 1) and is clamped to ``last page + 1``."""
        size = page_size if type(page_size) is int else AUDIT_PAGE_SIZE
        size = min(max(size, 1), AUDIT_PAGE_SIZE_MAX)
        try:
            wanted = int(page) if type(page) in (int, float) else 1
        except (OverflowError, ValueError):
            wanted = 1
        total = self.db.query("SELECT count(*) FROM audit")[0][0]
        page = min(max(wanted, 1), -(-total // size) + 1)  # past the last page: one empty page
        rows = self.db.query("SELECT id, at, event, actor, client_addr, detail_json FROM audit "
                             "ORDER BY id DESC LIMIT ? OFFSET ?", (size, (page - 1) * size))
        return AuditPage([AuditEntry(*row[:5], json.loads(row[5])) for row in rows], page, total)
