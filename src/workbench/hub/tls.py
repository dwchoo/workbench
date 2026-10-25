"""``wb hub tls --self-signed``: a self-signed cert and key for the web listener (Task Inbox SPEC "CLI 추가").

The host ``openssl`` does the work, invoked as an argument vector (never through a shell). Its
output is staged in a private ``0700`` directory and then installed next to the final paths:
the key ``0600`` (created that way, never widened), the cert ``0644``. Without ``force`` the
install uses ``link(2)``, which refuses an existing name, so a cert or key the user put there
is never overwritten, even by a race. ``force`` replaces the names themselves (``rename(2)``),
so a symlink at the target is replaced, not followed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile

from .config import WebConfig

DAYS = 825
OPENSSL_TIMEOUT = 60.0
KEY_MODE, CERT_MODE, DIR_MODE = 0o600, 0o644, 0o700
_CN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


class TlsError(Exception):
    """A refusal or failure; ``exit_code`` is the CLI exit status, ``text`` what to tell the user."""

    def __init__(self, text: str, exit_code: int = 1):
        super().__init__(text)
        self.text, self.exit_code = text, exit_code


@dataclass(frozen=True, slots=True)
class Generated:
    cert: Path
    key: Path
    fingerprint: str  # SHA-256 of the DER cert, ``AA:BB:...``
    san: tuple[str, ...]


def _common_name(bind: str) -> str:
    """The bind address, or the host name when it listens on every address."""
    if not ipaddress.ip_address(bind).is_unspecified:
        return bind
    name = socket.gethostname()
    return name if name and set(name) <= _CN_CHARS else "localhost"


def san_entries(web: WebConfig) -> tuple[str, ...]:
    return (f"IP:{web.bind}", "DNS:localhost", *(f"DNS:{name}" for name in web.server_names if name != "localhost"))


def _mkdir_private(directory: Path) -> None:
    if directory.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, DIR_MODE)


def _private_copy(source: Path, directory: Path, mode: int) -> Path:
    """A new file in ``directory`` with the bytes of ``source`` and ``mode`` from its creation."""
    descriptor, name = tempfile.mkstemp(prefix=".hub-tls-", dir=directory)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source.read_bytes())
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


def _install(source: Path, destination: Path, mode: int, force: bool) -> None:
    staged = _private_copy(source, destination.parent, mode)
    try:
        if force:
            os.replace(staged, destination)
        else:
            os.link(staged, destination)  # FileExistsError rather than overwriting
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass


def fingerprint_of(cert: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))


def self_signed(web: WebConfig, cert: Path, key: Path, *, force: bool, environment=None) -> Generated:
    environment = os.environ if environment is None else environment
    openssl = shutil.which("openssl", path=environment.get("PATH", os.defpath))
    if openssl is None:
        raise TlsError("openssl을 찾을 수 없습니다. openssl을 설치한 뒤 다시 실행하세요.", 2)
    existing = [path for path in (cert, key) if os.path.lexists(path)]
    if existing and not force:
        names = ", ".join(str(path) for path in existing)
        raise TlsError(f"이미 있어 덮어쓰지 않았습니다: {names}. 바꾸려면 '--force'를 붙여 다시 실행하세요.")
    san = san_entries(web)
    for directory in dict.fromkeys((key.parent, cert.parent)):
        try:
            _mkdir_private(directory)
        except OSError as exc:
            raise TlsError(f"디렉터리를 만들지 못했습니다: {directory}: {exc.strerror or exc}") from exc
    try:
        stage = Path(tempfile.mkdtemp(prefix=".hub-tls-", dir=key.parent))
    except OSError as exc:
        raise TlsError(f"임시 디렉터리를 만들지 못했습니다: {exc.strerror or exc}") from exc
    try:
        staged_key, staged_cert = stage / "key.pem", stage / "cert.pem"
        fd = os.open(staged_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_MODE)  # openssl keeps this mode
        os.close(fd)
        command = [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                   "-days", str(DAYS), "-subj", f"/CN={_common_name(web.bind)}",
                   "-addext", "subjectAltName=" + ",".join(san), "-keyout", str(staged_key), "-out", str(staged_cert)]
        try:
            run = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=OPENSSL_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TlsError(f"openssl을 실행하지 못했습니다: {exc}") from exc
        if run.returncode != 0 or not staged_cert.is_file() or staged_key.stat().st_size == 0:
            lines = (run.stderr or "").strip().splitlines()
            raise TlsError(f"openssl이 실패했습니다(exit {run.returncode}): {lines[-1] if lines else '출력 없음'}")
        try:
            _install(staged_key, key, KEY_MODE, force)
            try:
                _install(staged_cert, cert, CERT_MODE, force)
            except BaseException:
                if not force:
                    os.unlink(key)  # do not leave a key without its cert
                raise
        except FileExistsError as exc:
            raise TlsError(f"이미 있어 덮어쓰지 않았습니다: {exc.filename}. 바꾸려면 '--force'를 붙여 다시 실행하세요.") from exc
        except OSError as exc:
            raise TlsError(f"파일을 쓰지 못했습니다: {exc.strerror or exc}") from exc
        return Generated(cert, key, fingerprint_of(cert), san)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
