"""``wb hub tls --self-signed``: a self-signed cert and key for the web listener (Task Inbox SPEC "CLI 추가").

The host ``openssl`` does the work, invoked as an argument vector (never through a shell). Its
output is staged in a private ``0700`` directory and then installed next to the final paths:
the key ``0600``, the cert ``0644``. A name is only ever created, never overwritten, by the
install itself (``link(2)``, or ``O_CREAT | O_EXCL`` where hard links are unavailable), so a cert
or key the user put there is safe even from a race. ``force`` first moves the old pair aside
and puts it back if either new file cannot be installed: the two files are replaced as a pair
or not at all.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import ipaddress
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
from typing import Callable

from .config import WebConfig, _server_name

DAYS = 825
OPENSSL_TIMEOUT = 60.0
KEY_MODE, CERT_MODE, DIR_MODE = 0o600, 0o644, 0o700
SAN_MAX = 64
NO_HARD_LINKS = (errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK)
_CN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
# Added so a browser treats the cert as a TLS server leaf, not a CA.
EXTENSIONS = ("basicConstraints=critical,CA:FALSE", "extendedKeyUsage=serverAuth",
              "keyUsage=digitalSignature,keyEncipherment")


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


# --- subject alternative names ------------------------------------------------------------------

def _usable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (address.is_unspecified or address.is_multicast or address.is_link_local)


def proc_addresses(proc: Path = Path("/proc")) -> list[str]:
    """This host's interface addresses from ``net/fib_trie`` (IPv4) and ``net/if_inet6`` (IPv6); no tools run.

    Unspecified and link-local addresses are left out; a missing or odd file adds nothing.
    """
    found: list[str] = []
    try:
        last = None
        for line in (proc / "net" / "fib_trie").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("|--"):
                last = stripped[3:].strip()
            elif stripped.startswith("/32 host LOCAL") and last is not None:
                try:
                    address = ipaddress.IPv4Address(last)
                except ValueError:
                    continue
                if _usable(address) and str(address) not in found:
                    found.append(str(address))
    except (OSError, UnicodeDecodeError):
        pass
    try:
        for line in (proc / "net" / "if_inet6").read_text().splitlines():
            parts = line.split()
            if len(parts) < 4 or len(parts[0]) != 32:
                continue
            try:
                address = ipaddress.IPv6Address(bytes.fromhex(parts[0]))
            except ValueError:
                continue
            if _usable(address) and str(address) not in found:
                found.append(str(address))
    except (OSError, UnicodeDecodeError):
        pass
    return found


def discover_host() -> tuple[list[str], list[str]]:
    """Best-effort ``(addresses, DNS names)`` of this host: resolver answers for its name, then interfaces."""
    addresses: list[str] = []
    names: list[str] = []
    hostname = socket.gethostname()
    try:
        for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(hostname, None):
            if family in (socket.AF_INET, socket.AF_INET6):
                text = sockaddr[0].split("%", 1)[0]
                try:
                    if _usable(ipaddress.ip_address(text)):
                        addresses.append(str(ipaddress.ip_address(text)))
                except ValueError:
                    continue
    except OSError:
        pass
    addresses.extend(proc_addresses())
    candidates = [hostname]
    try:
        candidates.append(socket.getfqdn())
    except OSError:
        pass
    for candidate in candidates:
        name = _server_name(candidate)
        if name is not None and name != "localhost" and not name.replace(".", "").isdigit() and name not in names:
            names.append(name)
    return addresses, names


def san_entries(web: WebConfig, *, discover: Callable[[], tuple[list[str], list[str]]] = discover_host
                ) -> tuple[str, ...]:
    """``IP:...``/``DNS:...`` entries for ``web``: never the wildcard address itself.

    A wildcard bind (``0.0.0.0``/``::``) is reached through some host address, so it gets both
    loopback addresses plus the host's own addresses and names; a loopback bind gets both
    loopback addresses; any other bind gets that address. ``localhost`` and ``server_names``
    are always named.
    """
    bind = ipaddress.ip_address(web.bind)
    addresses: list[str] = []
    names: list[str] = []
    discovered: tuple[list[str], list[str]] = ([], [])
    if bind.is_unspecified:
        discovered = discover()
    if bind.is_unspecified or bind.is_loopback:
        addresses += ["127.0.0.1", "::1"]
    if not bind.is_unspecified:
        addresses.append(str(bind))
    for text in discovered[0]:
        try:
            addresses.append(str(ipaddress.ip_address(text)))
        except ValueError:
            continue
    names = ["localhost", *discovered[1], *web.server_names]
    entries = [f"IP:{address}" for address in dict.fromkeys(addresses)]
    entries += [f"DNS:{name}" for name in dict.fromkeys(names)]
    return tuple(entries[:SAN_MAX])


# --- installing the pair ---------------------------------------------------------------------------

def _mkdir_private(directory: Path) -> None:
    if directory.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, DIR_MODE)


def _install(source: Path, destination: Path, mode: int) -> None:
    """Create ``destination`` from ``source``; ``FileExistsError`` rather than overwriting.

    ``link(2)`` where the file system allows it; otherwise (``EPERM``/``EXDEV``/...) a new file
    opened ``O_CREAT | O_EXCL | O_NOFOLLOW`` and written, removed again if the write fails.
    """
    try:
        os.link(source, destination)
        return
    except OSError as exc:
        if exc.errno not in NO_HARD_LINKS:
            raise
    data = source.read_bytes()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(destination)
        except FileNotFoundError:
            pass
        raise


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _install_pair(staged_key: Path, staged_cert: Path, key: Path, cert: Path, force: bool) -> None:
    """Both files in place, or the destinations as they were before the call.

    With ``force`` the old files are renamed aside first (a symlink is moved, not followed) and
    renamed back if either install fails; the backups are deleted once both are in place.
    """
    backups: dict[Path, Path] = {}
    placed: list[Path] = []
    try:
        if force:
            for destination in (key, cert):
                if os.path.lexists(destination):
                    backup = destination.with_name(f".{destination.name}.bak-{secrets.token_hex(4)}")
                    os.rename(destination, backup)
                    backups[destination] = backup
        for staged, destination, mode in ((staged_key, key, KEY_MODE), (staged_cert, cert, CERT_MODE)):
            _install(staged, destination, mode)
            placed.append(destination)
    except BaseException:
        stuck = []
        for destination in reversed(placed):
            _unlink(destination)
        for destination, backup in backups.items():
            try:
                os.replace(backup, destination)
            except OSError:
                stuck.append(str(backup))
        if stuck:
            raise TlsError("복원하지 못했습니다. 이전 파일이 다음에 남아 있습니다: " + ", ".join(stuck)) from None
        raise
    for backup in backups.values():
        _unlink(backup)


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
                   "-addext", "subjectAltName=" + ",".join(san)]
        for extension in EXTENSIONS:
            command += ["-addext", extension]
        command += ["-keyout", str(staged_key), "-out", str(staged_cert)]
        try:
            run = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=OPENSSL_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TlsError(f"openssl을 실행하지 못했습니다: {exc}") from exc
        if run.returncode != 0 or not staged_cert.is_file() or staged_key.stat().st_size == 0:
            lines = (run.stderr or "").strip().splitlines()
            raise TlsError(f"openssl이 실패했습니다(exit {run.returncode}): {lines[-1] if lines else '출력 없음'}")
        os.chmod(staged_key, KEY_MODE)
        os.chmod(staged_cert, CERT_MODE)
        try:
            _install_pair(staged_key, staged_cert, key, cert, force)
        except FileExistsError as exc:
            raise TlsError(f"이미 있어 덮어쓰지 않았습니다: {exc.filename}. 바꾸려면 '--force'를 붙여 다시 실행하세요.") from exc
        except OSError as exc:
            raise TlsError(f"파일을 쓰지 못했습니다: {exc.strerror or exc}") from exc
        return Generated(cert, key, fingerprint_of(cert), san)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
