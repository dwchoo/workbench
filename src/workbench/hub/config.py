"""``hub.toml`` (Task Inbox SPEC "``hub.toml`` schema"): strict read, TLS decision, plain-HTTP warning.

The Hub reads the file once at start. It writes it only to create the initial template when nothing
is at the path (2026-10-26 user request; :func:`write_template`, which never replaces anything).
Anything it does not know (a key, a table, a type, a hostname ``bind``) makes the whole file
``config_invalid:<item>``: the web listener and Board agent stay closed while the rest of the Hub works.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import ipaddress
import os
from pathlib import Path
import ssl
import stat
import tomllib
from typing import Mapping

from workbench.backend.paths import write_private_bytes

APP_DIR_NAME = "omp-workbench"
CONFIG_NAME = "hub.toml"
CONFIG_FILE_LIMIT = 64 * 1024
TLS_DIR_NAME = "tls"
TLS_CERT_NAME, TLS_KEY_NAME = "hub-cert.pem", "hub-key.pem"
DEFAULT_BIND, DEFAULT_PORT = "127.0.0.1", 8780
INSECURE_WARNING_TEXT = "평문 HTTP로 LAN에 열려 있습니다. VPN 등 네트워크 보호가 필요합니다."
SERVER_NAME_MAX = 253
SERVER_NAMES_MAX = 64
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
CONFIG_FILE_MODE, CONFIG_DIR_MODE = 0o600, 0o700
_NO_HARD_LINKS = (errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK)

# The initial ``hub.toml`` (2026-10-26 user request: created automatically when absent). It parses
# to exactly ``HubConfig()``; every key but ``web.enabled`` is a comment showing its default.
CONFIG_TEMPLATE = """\
# OMP Workbench Hub 설정 파일 (hub.toml)
#
# Hub가 시작할 때 이 파일이 없으면 이 기본 내용으로 한 번 만듭니다. 그 뒤로 Hub는 이 파일을
# 고치지 않습니다. Hub는 시작할 때 한 번만 읽으므로, 고친 뒤에는 'wb hub stop' 뒤
# 'wb hub start'로 Hub를 재시작해야 반영됩니다.
# 모르는 키나 잘못된 값이 하나라도 있으면 파일 전체가 무효(config_invalid)가 되어 웹과
# Board agent는 열리지 않고, Hub의 나머지 기능(등록, 목록, 요청 전달)은 그대로 동작합니다.
# 아래에서 '#'으로 막힌 줄은 기본값을 보여 줍니다. 바꾸려면 '#'을 지우고 값을 고치세요.
#
# 웹(Task Inbox 화면, 외부 /mcp, Web Board) 켜는 방법:
#   1. 아래 [web]의 'enabled = false'를 'enabled = true'로 바꿉니다.
#   2. 'wb hub passwd'로 웹 로그인 비밀번호를 정합니다.
#   3. 'wb hub stop' 뒤 'wb hub start'로 Hub를 재시작합니다.
#
# 보안: bind를 LAN 주소나 0.0.0.0으로 바꾸면 같은 네트워크의 다른 기기도 접속할 수 있고,
# TLS 없이(평문 HTTP) 열면 비밀번호와 내용이 암호화되지 않으니 VPN으로 보호하거나
# 'wb hub tls --self-signed'로 HTTPS를 쓰세요.

[web]
# enabled: 웹을 열지 여부(true/false). 기본값 false(꺼짐).
enabled = false

# bind: 웹이 받을 IP 주소(IP 주소만, 호스트 이름은 안 됨). 기본값 "127.0.0.1"(이 컴퓨터에서만 접속).
# bind = "127.0.0.1"

# port: 웹 포트(0~65535, 0은 임의 포트). 기본값 8780.
# port = 8780

# tls_cert, tls_key: TLS 인증서와 key 파일의 절대 경로. 기본값 ""는 이 디렉터리의
# tls/hub-cert.pem, tls/hub-key.pem입니다('wb hub tls --self-signed'가 만듭니다).
# 둘 다 있으면 HTTPS로 열고, 하나만 있거나 읽지 못하면 열지 않습니다(평문으로 대신 열지 않음).
# tls_cert = ""
# tls_key = ""

# allow_insecure_http: TLS 파일이 없을 때 평문 HTTP로 열지 여부. 기본값 true.
# false면 TLS 파일 없이는 열지 않습니다.
# allow_insecure_http = true

# server_names: bind IP 외에 받을 접속 이름(Host) 목록(예: ["box.vpn"]). 기본값 [].
# server_names = []

[board_agent]
# model: Board agent가 쓸 OMP 모델. 기본값 ""(omp-models.yml의 manager 항목).
# model = ""

# role_prompt_file: 기본 역할 prompt 뒤에 덧붙일 파일의 절대 경로. 기본값 ""(없음).
# role_prompt_file = ""

# shell: Board terminal이 쓸 shell의 절대 경로. 기본값 ""($SHELL, 없으면 /bin/sh).
# shell = ""
"""


class ConfigInvalid(ValueError):
    """``hub.toml`` is unusable; ``item`` names the offending key (``web.bind``) or ``toml``/``file``."""

    def __init__(self, item: str):
        super().__init__(f"config_invalid:{item}")
        self.item = item

    @property
    def reason(self) -> str:
        return f"config_invalid:{self.item}"


@dataclass(frozen=True, slots=True)
class WebConfig:
    enabled: bool = False  # 2026-10-26 user decision: the web surface stays off until hub.toml turns it on
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    tls_cert: str = ""
    tls_key: str = ""
    allow_insecure_http: bool = True
    server_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoardAgentConfig:
    model: str = ""
    role_prompt_file: str = ""
    shell: str = ""


@dataclass(frozen=True, slots=True)
class HubConfig:
    web: WebConfig = field(default_factory=WebConfig)
    board_agent: BoardAgentConfig = field(default_factory=BoardAgentConfig)


def config_dir(environment: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/omp-workbench`` when absolute, else ``~/.config/omp-workbench``."""
    environment = os.environ if environment is None else environment
    base = environment.get("XDG_CONFIG_HOME", "")
    if not (base and os.path.isabs(base)):
        base = os.path.join(environment.get("HOME") or os.path.expanduser("~"), ".config")
    return Path(os.path.abspath(base)) / APP_DIR_NAME


def config_path(environment: Mapping[str, str] | None = None) -> Path:
    return config_dir(environment) / CONFIG_NAME


def _open_config_dir(directory: Path) -> int:
    """``directory`` as a validated fd: created ``0700`` when missing; ``ConfigInvalid('dir')`` when unsafe.

    Unsafe: a symlink or non-directory, another owner, or writable by group/others. An existing
    directory's mode is left as it is (only a directory created here is set to ``0700``).
    """
    created = False
    try:
        directory.parent.mkdir(parents=True, exist_ok=True, mode=CONFIG_DIR_MODE)
        os.mkdir(directory, CONFIG_DIR_MODE)
        created = True
    except FileExistsError:
        pass
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ConfigInvalid("dir") from None
        raise
    try:
        info = os.fstat(descriptor)
        if created and stat.S_IMODE(info.st_mode) != CONFIG_DIR_MODE:
            os.fchmod(descriptor, CONFIG_DIR_MODE)
            info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ConfigInvalid("dir")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _create_exclusive(directory_fd: int, payload: bytes) -> None:
    """``hub.toml`` from ``payload``, never replacing a name that exists (a symlink included).

    A private temp file hard-linked into place, so a reader never sees half a template; where the
    file system has no hard links, ``O_CREAT | O_EXCL | O_NOFOLLOW`` and a direct write.
    """
    try:
        write_private_bytes(Path(CONFIG_NAME), payload, dir_fd=directory_fd, replace_existing=False)
        return
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
    descriptor = os.open(CONFIG_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         CONFIG_FILE_MODE, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, CONFIG_FILE_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(CONFIG_NAME, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def write_template(environment: Mapping[str, str] | None = None) -> str:
    """Create ``hub.toml`` from :data:`CONFIG_TEMPLATE` when nothing is at :func:`config_path`.

    ``created``; ``exists`` (anything already there, also a symlink or a lost race: untouched);
    ``skipped:<reason>`` (``dir`` for an unsafe directory, else an errno name). Never raises.
    """
    try:
        directory_fd = _open_config_dir(config_dir(environment))
    except ConfigInvalid as exc:
        return f"skipped:{exc.item}"
    except (OSError, ValueError) as exc:
        return f"skipped:{_errno_name(exc)}"
    try:
        _create_exclusive(directory_fd, CONFIG_TEMPLATE.encode("utf-8"))
    except FileExistsError:
        return "exists"
    except (OSError, ValueError) as exc:
        return f"skipped:{_errno_name(exc)}"
    finally:
        os.close(directory_fd)
    return "created"


def _errno_name(exc: BaseException) -> str:
    number = getattr(exc, "errno", None)
    return errno.errorcode.get(number, "error") if number else type(exc).__name__


def tls_paths(web: WebConfig, environment: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """The cert/key the listener uses: the configured paths, ``""`` meaning the default TLS path."""
    tls_dir = config_dir(environment) / TLS_DIR_NAME
    return (Path(web.tls_cert) if web.tls_cert else tls_dir / TLS_CERT_NAME,
            Path(web.tls_key) if web.tls_key else tls_dir / TLS_KEY_NAME)


def _ip_literal(value: object) -> bool:
    if not isinstance(value, str) or "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _server_name(value: object) -> str | None:
    """A lower-cased DNS name (letters, digits, ``-``, dot-separated labels), else ``None``."""
    if not isinstance(value, str) or not value or len(value) > SERVER_NAME_MAX:
        return None
    name = value.lower()
    labels = name.split(".")
    if any(not label or len(label) > 63 or not set(label) <= _LABEL_CHARS
           or label.startswith("-") or label.endswith("-") for label in labels):
        return None
    return name


def _absolute_or_empty(value: object) -> bool:
    return isinstance(value, str) and (value == "" or (os.path.isabs(value) and "\0" not in value))


def _web(table: dict) -> WebConfig:
    values = {}
    for key, value in table.items():
        item = f"web.{key}"
        if key == "bind":
            if not _ip_literal(value):
                raise ConfigInvalid(item)
            values[key] = value
        elif key == "port":
            if type(value) is not int or not 0 <= value <= 65535:
                raise ConfigInvalid(item)
            values[key] = value
        elif key in ("tls_cert", "tls_key"):
            if not _absolute_or_empty(value):
                raise ConfigInvalid(item)
            values[key] = value
        elif key in ("enabled", "allow_insecure_http"):
            if type(value) is not bool:
                raise ConfigInvalid(item)
            values[key] = value
        elif key == "server_names":
            if not isinstance(value, list) or len(value) > SERVER_NAMES_MAX:
                raise ConfigInvalid(item)
            names = tuple(_server_name(name) for name in value)
            if None in names:
                raise ConfigInvalid(item)
            values[key] = tuple(dict.fromkeys(names))
        else:
            raise ConfigInvalid(item)
    return WebConfig(**values)


def _board_agent(table: dict) -> BoardAgentConfig:
    values = {}
    for key, value in table.items():
        item = f"board_agent.{key}"
        if key == "model":
            if not isinstance(value, str) or len(value) > 256:
                raise ConfigInvalid(item)
        elif key in ("role_prompt_file", "shell"):
            if not _absolute_or_empty(value):
                raise ConfigInvalid(item)
        else:
            raise ConfigInvalid(item)
        values[key] = value
    return BoardAgentConfig(**values)


_TABLES = {"web": _web, "board_agent": _board_agent}


def parse_config(data: bytes) -> HubConfig:
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ConfigInvalid("toml") from None
    parts = {}
    for name, table in document.items():
        reader = _TABLES.get(name)
        if reader is None or not isinstance(table, dict):
            raise ConfigInvalid(name)
        parts[name] = reader(table)
    return HubConfig(**parts)


def load_config(path: Path) -> HubConfig:
    """The defaults when ``path`` does not exist; :class:`ConfigInvalid` for anything unusable."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return HubConfig()
    except OSError:
        raise ConfigInvalid("file") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CONFIG_FILE_LIMIT:
            raise ConfigInvalid("file")
        data = os.read(descriptor, CONFIG_FILE_LIMIT + 1)
    except OSError:
        raise ConfigInvalid("file") from None
    finally:
        os.close(descriptor)
    if len(data) > CONFIG_FILE_LIMIT:
        raise ConfigInvalid("file")
    return parse_config(data)


@dataclass(frozen=True, slots=True)
class TlsDecision:
    """``tls`` (with ``context``), ``insecure_ok``, ``tls_invalid`` or ``insecure_http_disallowed``."""

    state: str
    context: ssl.SSLContext | None = None
    detail: str | None = None
    warnings: tuple[str, ...] = ()  # served anyway, shown in status (e.g. ``tls_key_mode:0644``)

    @property
    def opens(self) -> bool:
        return self.state in ("tls", "insecure_ok")


def decide_tls(web: WebConfig, cert: Path, key: Path) -> TlsDecision:
    """Both files: HTTPS or ``tls_invalid`` (never plain HTTP); neither: plain HTTP when allowed."""
    have_cert, have_key = os.path.lexists(cert), os.path.lexists(key)
    if not have_cert and not have_key:
        return TlsDecision("insecure_ok" if web.allow_insecure_http else "insecure_http_disallowed")
    if not (have_cert and have_key):
        return TlsDecision("tls_invalid", detail="cert_without_key" if have_cert else "key_without_cert")
    try:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
    except (OSError, ssl.SSLError, ValueError):
        return TlsDecision("tls_invalid", detail="load_failed")
    warnings = []
    try:
        mode = stat.S_IMODE(os.stat(key).st_mode)
    except OSError:
        mode = None
    if mode is not None and mode != 0o600:
        warnings.append(f"tls_key_mode:{mode:04o}")  # SPEC: key 0600; a warning, not a refusal
    return TlsDecision("tls", context, warnings=tuple(warnings))


def is_loopback(bind: str) -> bool:
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def insecure_warning(bind: str, *, tls: bool) -> bool:
    """Plain HTTP on anything but loopback (``0.0.0.0``/``::`` included) warns."""
    return not tls and not is_loopback(bind)
