"""Shared sandbox for Task Inbox web tests that run a real ``wb _hub --foreground`` (seam T3).

Usage (from a test under ``tests/hub``; ``discover -s tests/hub`` puts ``tests/hub`` on sys.path)::

    from fixtures.web_sandbox import WebSandbox, PASSWORD
    box = WebSandbox(self)                 # temp HOME/XDG_*, short private XDG_RUNTIME_DIR, AUTOSTART=0
    box.write_config(bind='127.0.0.1')     # hub.toml [web] enabled, port 0 (pass ``extra`` lines as needed)
    box.set_password()                     # ``wb hub passwd --stdin`` (no Hub needed)
    hub = box.start_hub()                  # waits for hub.json + socket
    host, port = box.wait_listening(hub.pid)   # the Hub's own LISTEN socket from /proc/net/tcp*
    box.status()['web']                    # ``wb hub status --json``
    box.stop_hub(hub)                      # SIGTERM, wait, return code

Everything the sandbox starts is stopped in ``cleanup`` (registered with ``addCleanup``) and the
temp directories are removed. Tests bind 127.0.0.0/8 only, with port 0.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[3]
PASSWORD = 'correct horse battery staple'


def wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _socket_inodes(pid: int) -> set[int]:
    inodes = set()
    try:
        names = os.listdir(f'/proc/{pid}/fd')
    except OSError:
        return inodes
    for name in names:
        try:
            target = os.readlink(f'/proc/{pid}/fd/{name}')
        except OSError:
            continue
        if target.startswith('socket:['):
            inodes.add(int(target[8:-1]))
    return inodes


def _decode_v4(text: str) -> str:
    return str(ipaddress.IPv4Address(bytes.fromhex(text)[::-1]))


def _decode_v6(text: str) -> str:
    raw = bytes.fromhex(text)
    return str(ipaddress.IPv6Address(b''.join(raw[i:i + 4][::-1] for i in range(0, 16, 4))))


def tcp_listening(pid: int) -> list[tuple[str, int]]:
    """``(address, port)`` of every TCP LISTEN socket ``pid`` holds, read from ``/proc/net/tcp*``."""
    inodes = _socket_inodes(pid)
    found = []
    for table, decode in (('/proc/net/tcp', _decode_v4), ('/proc/net/tcp6', _decode_v6)):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            parts = row.split()
            if len(parts) < 10 or parts[3] != '0A' or int(parts[9]) not in inodes:
                continue
            address, port = parts[1].split(':')
            found.append((decode(address), int(port, 16)))
    return found


class WebSandbox:
    """Temp HOME/XDG dirs, ``hub.toml``/password helpers and the Hub processes one test owns."""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.temp = tempfile.TemporaryDirectory(prefix='wb-web-')
        self.base = Path(self.temp.name)
        # A short private runtime dir: a long one would make the runtime fall back to /tmp.
        self.runtime = Path(tempfile.mkdtemp(prefix='wr'))
        os.chmod(self.runtime, 0o700)
        self.home = self.base / 'home'
        self.state_home = self.base / 'state'
        self.config_home = self.base / 'config'
        for directory in (self.home, self.state_home, self.config_home):
            directory.mkdir(mode=0o700)
        self.state = self.state_home / 'omp-workbench' / 'hub'
        self.config_dir = self.config_home / 'omp-workbench'
        self.config_file = self.config_dir / 'hub.toml'
        self.tls_dir = self.config_dir / 'tls'
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), 'LANG': 'C.UTF-8',
                    'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'XDG_STATE_HOME': str(self.state_home), 'XDG_RUNTIME_DIR': str(self.runtime),
                    'XDG_CONFIG_HOME': str(self.config_home), 'WORKBENCH_HUB_AUTOSTART': '0'}
        self.processes: list[subprocess.Popen] = []
        case.addCleanup(self.cleanup)

    # --- configuration ------------------------------------------------------------------------

    def write_config(self, bind: str = '127.0.0.1', port: int = 0, *extra: str, raw: str | None = None,
                     enabled: bool | None = True) -> Path:
        """``hub.toml`` with ``[web] enabled/bind/port`` plus ``extra`` lines in ``[web]`` (or ``raw`` as is).

        ``enabled = true`` by default (the web surface is off without it, 2026-10-26 user decision);
        ``enabled=None`` leaves the key out."""
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        switch = [] if enabled is None else [f'enabled = {"true" if enabled else "false"}']
        text = raw if raw is not None else '\n'.join(['[web]', *switch, f'bind = "{bind}"', f'port = {port}', *extra,
                                                       ''])
        self.config_file.write_text(text)
        return self.config_file

    def install_tls(self) -> tuple[Path, Path]:
        """A throwaway self-signed pair at the default TLS paths (host openssl)."""
        self.tls_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        cert, key = self.tls_dir / 'hub-cert.pem', self.tls_dir / 'hub-key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                        '-nodes', '-days', '2', '-subj', '/CN=127.0.0.1', '-addext',
                        'subjectAltName=IP:127.0.0.1,DNS:localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True, timeout=30)
        os.chmod(key, 0o600)
        return cert, key

    def set_password(self, password: str = PASSWORD) -> subprocess.CompletedProcess:
        result = self.cli('hub', 'passwd', '--stdin', input_text=password + '\n')
        self.case.assertEqual(result.returncode, 0, result)
        return result

    # --- processes ----------------------------------------------------------------------------

    def cli(self, *args: str, timeout: float = 40, input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, '-m', 'workbench', *args], env=self.env, cwd=self.base,
                              input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def status(self) -> dict:
        result = self.cli('hub', 'status', '--json')
        self.case.assertEqual(result.returncode, 0, result)
        return json.loads(result.stdout)

    def start_hub(self) -> subprocess.Popen:
        """A foreground Hub; returns once its record names it and its socket exists."""
        log = open(self.base / f'hub-{len(self.processes)}.log', 'w')
        process = subprocess.Popen([sys.executable, '-m', 'workbench', '_hub', '--foreground'], env=self.env,
                                   cwd=self.base, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        log.close()
        self.processes.append(process)
        record = self.state / 'hub.json'

        def ready() -> bool:
            if process.poll() is not None:
                return True
            try:
                value = json.loads(record.read_text())
            except (OSError, ValueError):
                return False
            return value.get('pid') == process.pid and Path(value.get('socket', '')).exists()

        if not wait_until(ready, 15) or process.poll() is not None:
            self.case.fail(f'Hub did not become ready: rc={process.poll()} log={self.hub_log(process)!r}')
        return process

    def hub_log(self, process: subprocess.Popen) -> str:
        index = self.processes.index(process)
        try:
            return (self.base / f'hub-{index}.log').read_text()
        except OSError:
            return ''

    def wait_listening(self, pid: int, timeout: float = 10) -> tuple[str, int]:
        found: list = []
        wait_until(lambda: bool(found.extend(tcp_listening(pid)) or found), timeout)
        self.case.assertEqual(len(found), 1, f'expected one LISTEN socket of {pid}: {found}')
        return found[0]

    def stop_hub(self, process: subprocess.Popen, timeout: float = 15) -> int:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        return process.wait(timeout=timeout)

    def cleanup(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.kill(process.pid, signal.SIGCONT)
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass
        self.temp.cleanup()
        for root, dirs, files in os.walk(self.runtime, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                path = os.path.join(root, name)
                (os.unlink if os.path.islink(path) else os.rmdir)(path)
        os.rmdir(self.runtime)


def free_port_holder(host: str = '127.0.0.1') -> socket.socket:
    """A listening socket on ``host`` (ephemeral port) that the caller closes: a busy port for bind tests."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind((host, 0))
    holder.listen(1)
    return holder
