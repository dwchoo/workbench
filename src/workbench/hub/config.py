"""``hub.toml`` (Task Inbox SPEC "``hub.toml`` schema"): strict read, TLS decision, plain-HTTP warning.

The Hub reads the file once at start and never writes it. Anything it does not know (a key,
a table, a type, a hostname ``bind``) makes the whole file ``config_invalid:<item>``: the web
listener and Board agent stay closed while the rest of the Hub works.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import ssl
import stat
import tomllib
from typing import Mapping

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
