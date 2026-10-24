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
    return None


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
        if record is None or not isinstance(password, str) or len(password) > PASSWORD_MAX:
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
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
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
        self.db.write(lambda execute: execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_token_hash(token), now, expires_at, generation, csrf, client_addr, user_agent)))
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

    def reissue(self, token: str, now: float) -> "Reissued | None":
        """After a web password change: replace this unexpired login with one bound to the
        current generation (same expiry, new token and CSRF) and delete every other session."""
        try:
            generation = self.passwords.generation()
        except AuthInvalid:
            return None
        old = self._row(token)
        if generation is None or old is None or not now < old.expires_at:
            return None
        fresh = IssuedSession(_token(), _token(), old.expires_at)

        def steps(execute):
            if not execute("DELETE FROM sessions WHERE token_hash = ?", (old.token_hash,)).rowcount:
                return None
            invalidated = execute("DELETE FROM sessions").rowcount
            execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_token_hash(fresh.token), old.created_at, old.expires_at, generation, fresh.csrf,
                     old.client_addr, old.user_agent))
            return Reissued(fresh, invalidated)
        return self.db.write(steps)


@dataclass(frozen=True, slots=True)
class Reissued:
    session: IssuedSession
    invalidated: int  # other sessions deleted (the audit ``password_changed`` detail)


# --- login failure delay ----------------------------------------------------------------------

DELAY_CAP = 30
GLOBAL_KEY = "global"


def delay_seconds(failures: int) -> int:
    """SPEC ``f(n)``: 0 for n <= 3, else ``min(2^(n-4), 30)`` seconds."""
    return 0 if failures <= 3 else min(2 ** min(failures - 4, 5), DELAY_CAP)


class LoginThrottle:
    """Consecutive login failures per client address and Hub-wide (``login_state``, kept across restarts).

    The wait before the next verification is ``max(f(addr), f(global // 3))``.
    """

    def __init__(self, db: HubDb, *, clock=time.time):
        self.db, self._clock = db, clock

    @staticmethod
    def _key(addr: str | None) -> str:
        return f"addr:{addr or ''}"

    def _failures(self, key: str) -> int:
        rows = self.db.query("SELECT failures FROM login_state WHERE key = ?", (key,))
        return rows[0][0] if rows else 0

    def delay_for(self, addr: str | None) -> int:
        return max(delay_seconds(self._failures(self._key(addr))),
                   delay_seconds(self._failures(GLOBAL_KEY) // 3))

    def record_failure(self, addr: str | None) -> None:
        now = float(self._clock())

        def steps(execute):
            for key in (self._key(addr), GLOBAL_KEY):
                execute("INSERT INTO login_state VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE "
                        "SET failures = failures + 1, updated_at = excluded.updated_at", (key, now))
        self.db.write(steps)

    def record_success(self, addr: str | None) -> None:
        self.db.write(lambda execute: execute("DELETE FROM login_state WHERE key IN (?, ?)",
                                              (self._key(addr), GLOBAL_KEY)))


# --- API keys ---------------------------------------------------------------------------------

KEY_PREFIX = "wbk_"
KEY_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # RFC 4648 base32, lower case
KEY_ID_LENGTH, KEY_SECRET_LENGTH = 10, 43
KEY_LENGTH = len(KEY_PREFIX) + KEY_ID_LENGTH + 1 + KEY_SECRET_LENGTH
_BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
GRADES = ("requester", "editor")
SYSTEM_KEY_NAME = "board-agent"
KEY_NAME_MAX = 64
_KEY_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._-")
KEY_EXPIRY_DAYS_MAX = 3650
PROJECT_ID_MAX = 128
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
        if (projects and all(isinstance(item, str) and 0 < len(item) <= PROJECT_ID_MAX for item in projects)
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
        while True:
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


def _check_value(value: object, depth: int = 0) -> None:
    if value is None or type(value) in (bool, int, float):
        return
    if isinstance(value, str):
        if len(value) > AUDIT_TEXT_LIMIT:
            raise AuditRejected("detail value too long")
        return
    if depth == 0 and isinstance(value, (list, dict)):  # e.g. an API key scope
        items = value.values() if isinstance(value, dict) else value
        if len(value) > 64 or (isinstance(value, dict) and not all(isinstance(k, str) for k in value)):
            raise AuditRejected("detail value too large")
        for item in items:
            _check_value(item, depth + 1)
        return
    raise AuditRejected("unsupported detail value")


class AuditLog:
    """``hub.sqlite3.audit``: the newest :data:`AUDIT_LIMIT` rows are kept."""

    def __init__(self, db: HubDb, *, limit: int = AUDIT_LIMIT):
        self.db, self.limit = db, limit

    def record(self, event: str, actor: str, client_addr: str | None, detail: dict, *, now: float) -> None:
        allowed = AUDIT_EVENTS.get(event)
        if allowed is None:
            raise AuditRejected(f"unknown audit event {event!r}")
        if not isinstance(detail, dict) or not set(detail) <= allowed:
            raise AuditRejected(f"detail fields not allowed for {event}")
        for value in detail.values():
            _check_value(value)
        for text in (actor, client_addr):
            if text is not None and (not isinstance(text, str) or len(text) > AUDIT_TEXT_LIMIT):
                raise AuditRejected("actor/client_addr must be short text")
        payload = json.dumps(detail, sort_keys=True, ensure_ascii=False)

        def steps(execute):
            execute("INSERT INTO audit (at, event, actor, client_addr, detail_json) VALUES (?, ?, ?, ?, ?)",
                    (now, event, actor, client_addr, payload))
            execute("DELETE FROM audit WHERE id IN (SELECT id FROM audit ORDER BY id DESC LIMIT -1 OFFSET ?)",
                    (self.limit,))
        self.db.write(steps)

    def list_audit(self, page: int = 1, *, page_size: int = AUDIT_PAGE_SIZE) -> AuditPage:
        """Newest first; ``page`` starts at 1."""
        page = max(1, int(page))
        total = self.db.query("SELECT count(*) FROM audit")[0][0]
        rows = self.db.query("SELECT id, at, event, actor, client_addr, detail_json FROM audit "
                             "ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, (page - 1) * page_size))
        return AuditPage([AuditEntry(*row[:5], json.loads(row[5])) for row in rows], page, total)
