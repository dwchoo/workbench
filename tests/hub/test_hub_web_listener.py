"""TI-03 web listener: ``hub.toml`` checks and TLS decision (functions), then the listener's
lifetime inside a real Hub (seam T3) and inside an in-process loop.

Expected values come from the SPEC ("``hub.toml`` schema", "Web listener 수명"), from
``/proc/net/tcp*`` (bind address and port of the Hub's own LISTEN sockets) and from stdlib
``http.client``/``ssl`` responses. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import http.client
import asyncio
import json
import os
import socket
import sqlite3
import ssl
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workbench.hub import config  # noqa: E402
from fixtures.web_sandbox import WebSandbox, tcp_listening, wait_until  # noqa: E402

WARNING = '평문 HTTP로 LAN에 열려 있습니다. VPN 등 네트워크 보호가 필요합니다.'


def self_signed(directory: Path, name: str = 'hub') -> tuple[Path, Path]:
    """A throwaway self-signed cert/key pair made with the host openssl."""
    cert, key = directory / f'{name}-cert.pem', directory / f'{name}-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                    '-nodes', '-days', '2', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True, timeout=30)
    os.chmod(key, 0o600)
    return cert, key


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wb-cfg-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def parse(self, text: str) -> config.HubConfig:
        return config.parse_config(text.encode('utf-8'))

    def assert_invalid(self, text: str, item: str) -> None:
        with self.assertRaises(config.ConfigInvalid) as caught:
            self.parse(text)
        self.assertEqual(caught.exception.item, item)
        self.assertEqual(caught.exception.reason, f'config_invalid:{item}')


class HubTomlTests(ConfigCase):
    def test_missing_file_gives_the_spec_defaults(self):
        loaded = config.load_config(self.base / 'nope.toml')
        web = loaded.web
        self.assertEqual((web.bind, web.port, web.tls_cert, web.tls_key, web.allow_insecure_http, web.server_names),
                         ('127.0.0.1', 8780, '', '', True, ()))
        self.assertEqual((loaded.board_agent.model, loaded.board_agent.role_prompt_file, loaded.board_agent.shell),
                         ('', '', ''))

    def test_valid_file_is_read(self):
        path = self.base / 'hub.toml'
        path.write_text('[web]\nbind = "127.0.0.2"\nport = 0\nallow_insecure_http = false\n'
                        'server_names = ["Box.VPN"]\n[board_agent]\nmodel = "m"\n')
        loaded = config.load_config(path)
        self.assertEqual((loaded.web.bind, loaded.web.port, loaded.web.allow_insecure_http),
                         ('127.0.0.2', 0, False))
        self.assertEqual(loaded.web.server_names, ('box.vpn',))
        self.assertEqual(loaded.board_agent.model, 'm')

    def test_unknown_keys_and_tables_are_invalid(self):
        self.assert_invalid('[web]\nbnid = "127.0.0.1"\n', 'web.bnid')
        self.assert_invalid('[webb]\n', 'webb')
        self.assert_invalid('[board_agent]\ncolor = "x"\n', 'board_agent.color')
        self.assert_invalid('port = 1\n', 'port')

    def test_hostname_bind_and_bad_values_are_invalid(self):
        self.assert_invalid('[web]\nbind = "localhost"\n', 'web.bind')
        self.assert_invalid('[web]\nbind = "box.vpn"\n', 'web.bind')
        self.assert_invalid('[web]\nbind = ""\n', 'web.bind')
        self.assert_invalid('[web]\nport = 65536\n', 'web.port')
        self.assert_invalid('[web]\nport = -1\n', 'web.port')
        self.assert_invalid('[web]\nport = "8780"\n', 'web.port')
        self.assert_invalid('[web]\nport = true\n', 'web.port')
        self.assert_invalid('[web]\nallow_insecure_http = "yes"\n', 'web.allow_insecure_http')
        self.assert_invalid('[web]\nserver_names = "box"\n', 'web.server_names')
        self.assert_invalid('[web]\nserver_names = ["bad name"]\n', 'web.server_names')
        self.assert_invalid('[web]\ntls_cert = "relative.pem"\n', 'web.tls_cert')
        self.assert_invalid('[web]\nbind = "127.0.0.1"\nbind = "127.0.0.2"\n', 'toml')
        self.assert_invalid('[web\n', 'toml')
        self.assert_invalid('web = 1\n', 'web')

    def test_explicit_any_address_is_accepted(self):
        self.assertEqual(self.parse('[web]\nbind = "0.0.0.0"\n').web.bind, '0.0.0.0')
        self.assertEqual(self.parse('[web]\nbind = "::"\n').web.bind, '::')

    def test_config_path_follows_xdg_config_home(self):
        self.assertEqual(config.config_path({'XDG_CONFIG_HOME': '/x/cfg', 'HOME': '/h'}),
                         Path('/x/cfg/omp-workbench/hub.toml'))
        self.assertEqual(config.config_path({'XDG_CONFIG_HOME': 'rel', 'HOME': '/h'}),
                         Path('/h/.config/omp-workbench/hub.toml'))
        default = config.parse_config(b'')
        self.assertEqual(config.tls_paths(default.web, {'XDG_CONFIG_HOME': '/x/cfg', 'HOME': '/h'}),
                         (Path('/x/cfg/omp-workbench/tls/hub-cert.pem'), Path('/x/cfg/omp-workbench/tls/hub-key.pem')))


class TlsDecisionTests(ConfigCase):
    def decide(self, text: str, cert: Path, key: Path) -> config.TlsDecision:
        return config.decide_tls(self.parse(text).web, cert, key)

    def test_no_files_and_default_is_plain_http(self):
        decision = self.decide('', self.base / 'c.pem', self.base / 'k.pem')
        self.assertEqual(decision.state, 'insecure_ok')
        self.assertIsNone(decision.context)

    def test_no_files_and_insecure_disallowed_opens_nothing(self):
        decision = self.decide('[web]\nallow_insecure_http = false\n', self.base / 'c.pem', self.base / 'k.pem')
        self.assertEqual(decision.state, 'insecure_http_disallowed')

    def test_only_one_file_is_tls_invalid(self):
        cert, key = self_signed(self.base)
        self.assertEqual(self.decide('', cert, self.base / 'missing.pem').state, 'tls_invalid')
        self.assertEqual(self.decide('', self.base / 'missing.pem', key).state, 'tls_invalid')

    def test_unloadable_pair_is_tls_invalid(self):
        cert, key = self_signed(self.base)
        other_cert, _other_key = self_signed(self.base, 'other')
        junk = self.base / 'junk.pem'
        junk.write_text('not a key')
        self.assertEqual(self.decide('', cert, junk).state, 'tls_invalid')
        self.assertEqual(self.decide('', other_cert, key).state, 'tls_invalid')  # key does not match

    def test_both_files_give_a_tls_context_of_at_least_1_2(self):
        import ssl
        cert, key = self_signed(self.base)
        decision = self.decide('[web]\nallow_insecure_http = false\n', cert, key)
        self.assertEqual(decision.state, 'tls')
        self.assertIsInstance(decision.context, ssl.SSLContext)
        self.assertGreaterEqual(decision.context.minimum_version, ssl.TLSVersion.TLSv1_2)


class InsecureWarningTests(unittest.TestCase):
    def test_plain_http_off_loopback_warns(self):
        for bind in ('10.8.0.2', '0.0.0.0', '::', '192.168.1.5', 'fd00::1'):
            self.assertTrue(config.insecure_warning(bind, tls=False), bind)

    def test_loopback_or_tls_does_not_warn(self):
        for bind in ('127.0.0.1', '127.0.0.2', '127.255.0.1', '::1'):
            self.assertFalse(config.insecure_warning(bind, tls=False), bind)
        self.assertFalse(config.insecure_warning('10.8.0.2', tls=True))

    def test_warning_text_is_the_spec_sentence(self):
        self.assertEqual(config.INSECURE_WARNING_TEXT, WARNING)


class StatusLineTests(unittest.TestCase):
    """``wb hub status`` text for the ``web`` field (the JSON is hub_v1 as is)."""

    def lines(self, web: dict) -> list[str]:
        from workbench.hub.cli import web_status_lines
        return web_status_lines(web)

    def test_listening_line_and_plain_http_warning(self):
        self.assertEqual(self.lines({'state': 'listening', 'url': 'https://10.8.0.2:8780', 'tls': True,
                                     'insecure_warning': False}), ['web: listening https://10.8.0.2:8780'])
        lines = self.lines({'state': 'listening', 'url': 'http://10.8.0.2:8780', 'tls': False,
                            'insecure_warning': True})
        self.assertEqual(lines[0], 'web: listening http://10.8.0.2:8780')
        self.assertIn(WARNING, lines[1])

    def test_closed_states_name_the_reason(self):
        self.assertEqual(self.lines({'state': 'password_not_set'}),
                         ["web: not_listening (password_not_set) — 'wb hub passwd'로 비밀번호를 정하세요"])
        self.assertEqual(self.lines({'state': 'config_invalid', 'detail': 'web.bind'}),
                         ['web: not_listening (config_invalid:web.bind)'])
        self.assertEqual(self.lines({'state': 'bind_failed', 'detail': 'EADDRINUSE'}),
                         ['web: not_listening (bind_failed:EADDRINUSE)'])


class HubListenerTests(unittest.TestCase):
    """A real foreground Hub in the shared web sandbox (T3)."""

    def setUp(self):
        self.box = WebSandbox(self)

    def test_without_a_password_the_hub_listens_on_no_tcp_port_and_says_why(self):
        self.box.write_config()
        hub = self.box.start_hub()
        time.sleep(0.5)  # give a wrong implementation the chance to open
        self.assertEqual(tcp_listening(hub.pid), [])
        status = self.box.status()
        self.assertEqual(status['web']['state'], 'password_not_set')
        self.assertEqual(status['registry']['state'], 'ok')
        listed = self.box.cli('hub', 'list', '--json')
        self.assertEqual(listed.returncode, 0, listed)
        self.assertEqual(json.loads(listed.stdout)['rows'], [])
        self.assertEqual(tcp_listening(hub.pid), [])

    def test_password_set_while_running_opens_the_listener_within_five_seconds(self):
        self.box.write_config()
        hub = self.box.start_hub()
        self.assertEqual(tcp_listening(hub.pid), [])
        self.box.set_password()
        started = time.monotonic()
        self.assertTrue(wait_until(lambda: tcp_listening(hub.pid), 5.0), 'listener did not open within 5 s')
        self.assertLessEqual(time.monotonic() - started, 5.0)
        host, port = self.box.wait_listening(hub.pid)
        self.assertEqual(host, '127.0.0.1')
        web = self.box.status()['web']
        self.assertEqual(web['state'], 'listening')
        self.assertEqual(web['url'], f'http://127.0.0.1:{port}')
        text = self.box.cli('hub', 'status')
        self.assertIn(f'web: listening http://127.0.0.1:{port}', text.stdout)
        self.assertNotIn('평문 HTTP', text.stdout)
        self.assertIs(web['tls'], False)
        self.assertIs(web['insecure_warning'], False)
        self.assertIsNone(hub.poll())

    def test_configured_bind_address_is_the_only_one_and_port_zero_is_reported(self):
        self.box.write_config(bind='127.0.0.2', port=0)
        self.box.set_password()
        hub = self.box.start_hub()
        host, port = self.box.wait_listening(hub.pid)
        self.assertEqual(host, '127.0.0.2')
        self.assertNotEqual(port, 0)
        web = self.box.status()['web']
        self.assertEqual((web['state'], web['url'], web['bind'], web['port']),
                         ('listening', f'http://127.0.0.2:{port}', '127.0.0.2', port))
        with socket.create_connection(('127.0.0.2', port), timeout=5):
            pass
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=2).close()

    def test_self_signed_pair_at_the_default_path_serves_https(self):
        self.box.write_config()
        self.box.install_tls()
        self.box.set_password()
        hub = self.box.start_hub()
        _host, port = self.box.wait_listening(hub.pid)
        web = self.box.status()['web']
        self.assertEqual((web['state'], web['url'], web['tls'], web['insecure_warning']),
                         ('listening', f'https://127.0.0.1:{port}', True, False))
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=10, context=context)
        try:
            connection.request('GET', '/login', headers={'Host': f'127.0.0.1:{port}'})
            response = connection.getresponse()
            response.read()
            self.assertGreaterEqual(response.status, 200)
            self.assertIsNotNone(connection.sock.version())
        finally:
            connection.close()
        plain = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            with self.assertRaises((OSError, http.client.HTTPException)):
                plain.request('GET', '/login')
                plain.getresponse().read()
        finally:
            plain.close()

    def test_insecure_http_disallowed_without_tls_files_opens_nothing(self):
        self.box.write_config('127.0.0.1', 0, 'allow_insecure_http = false')
        self.box.set_password()
        hub = self.box.start_hub()
        self.assertEqual(self.box.status()['web']['state'], 'insecure_http_disallowed')
        time.sleep(2.5)  # one watchdog tick after the password exists
        self.assertEqual(tcp_listening(hub.pid), [])
        self.assertEqual(self.box.status()['web']['state'], 'insecure_http_disallowed')

    def test_cert_without_key_is_tls_invalid_and_never_falls_back_to_http(self):
        self.box.write_config()
        _cert, key = self.box.install_tls()
        key.unlink()
        self.box.set_password()
        hub = self.box.start_hub()
        self.assertEqual(self.box.status()['web']['state'], 'tls_invalid')
        time.sleep(2.5)
        self.assertEqual(tcp_listening(hub.pid), [])

    def test_invalid_hub_toml_keeps_the_listener_closed_and_the_hub_working(self):
        self.box.write_config('127.0.0.1', 0, 'colour = "blue"')
        self.box.set_password()
        hub = self.box.start_hub()
        status = self.box.status()
        self.assertEqual(status['web'], {'state': 'config_invalid', 'detail': 'web.colour'})
        self.assertEqual(status['registry']['state'], 'ok')
        time.sleep(2.5)
        self.assertEqual(tcp_listening(hub.pid), [])

    def test_sigterm_releases_the_port_and_the_hub_exits_cleanly(self):
        self.box.write_config()
        self.box.set_password()
        hub = self.box.start_hub()
        _host, port = self.box.wait_listening(hub.pid)
        idle = socket.create_connection(('127.0.0.1', port), timeout=5)  # an open keep-alive connection
        try:
            started = time.monotonic()
            self.assertEqual(self.box.stop_hub(hub), 0, self.box.hub_log(hub))
            self.assertLess(time.monotonic() - started, 10.0)
        finally:
            idle.close()
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=2).close()
        self.assertFalse((self.box.state / 'hub.json').exists())


class InProcessListenerTests(unittest.IsolatedAsyncioTestCase):
    """``WebListener`` on a real loop and real loopback sockets, with an injected retry clock."""

    async def asyncSetUp(self):
        from workbench.hub import auth
        from workbench.hub.web.listener import WebListener
        self.temp = tempfile.TemporaryDirectory(prefix='wb-lst-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'hub'
        auth.PasswordStore(self.state).set_password('correct horse battery')
        self.now = [1000.0]
        self.WebListener = WebListener

    def listener(self, port: int):
        loaded = config.parse_config(f'[web]\nbind = "127.0.0.1"\nport = {port}\n'.encode())
        decision = config.decide_tls(loaded.web, Path('/nonexistent/c'), Path('/nonexistent/k'))
        web = self.WebListener(self.state, config=loaded, config_error=None, tls=decision,
                               monotonic=lambda: self.now[0], bind_retry=30.0)
        self.addAsyncCleanup(web.stop)
        return web

    async def test_bind_failure_names_the_errno_and_is_retried_after_the_interval(self):
        holder = socket.socket()
        holder.bind(('127.0.0.1', 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        web = self.listener(port)
        try:
            await web.start()
            self.assertEqual(web.status()['state'], 'bind_failed')
            self.assertEqual(web.status()['detail'], 'EADDRINUSE')
        finally:
            holder.close()
        self.now[0] += 29.0
        await web.poll()
        self.assertEqual(web.status()['state'], 'bind_failed')  # not yet due
        self.now[0] += 1.5
        await web.poll()
        self.assertEqual(web.status()['state'], 'listening')
        self.assertEqual(web.status()['port'], port)
        with socket.create_connection(('127.0.0.1', port), timeout=5):
            pass

    async def test_password_generations_are_audited_as_cli_at_start_and_when_auth_json_changes(self):
        from workbench.hub import auth
        listener = self.listener(0)
        await listener.start()
        self.assertEqual(listener.status()['state'], 'listening')

        def changes():
            connection = sqlite3.connect(self.state / 'hub.sqlite3')
            try:
                return [(actor, json.loads(detail)) for actor, detail in connection.execute(
                    "SELECT actor, detail_json FROM audit WHERE event = 'password_changed' ORDER BY id")]
            finally:
                connection.close()
        self.assertEqual(changes(), [('cli', {'source': 'cli', 'invalidated_sessions': 0})])
        await listener.poll()
        self.assertEqual(len(changes()), 1)  # unchanged auth.json: nothing new
        services = listener.services
        await asyncio.get_running_loop().run_in_executor(None, lambda: services.sessions.create(
            '127.0.0.1', 'ua', 1000.0, generation=services.passwords.generation()))
        await asyncio.get_running_loop().run_in_executor(
            None, auth.PasswordStore(self.state).set_password, 'another good password')
        await listener.poll()
        self.assertEqual(changes()[1:], [('cli', {'source': 'cli', 'invalidated_sessions': 1})])
        self.assertEqual(listener.status()['state'], 'listening')

    async def test_a_corrupt_auth_json_never_opens_the_listener(self):
        (self.state / 'auth.json').write_text('{"schema_version": 1}')
        os.chmod(self.state / 'auth.json', 0o600)
        listener = self.listener(0)
        await listener.start()
        self.assertEqual(listener.status(), {'state': 'password_not_set', 'detail': 'auth_invalid'})
        self.assertIsNone(listener.runner)
        os.chmod(self.state / 'auth.json', 0o644)
        await listener.poll()
        self.assertEqual(listener.status()['state'], 'password_not_set')
        self.assertIsNone(listener.runner)

    async def test_stop_closes_the_site_then_websockets_1001_then_hooks_then_flushes_key_use(self):
        from aiohttp import ClientSession, WSMsgType, web
        from workbench.hub import auth
        from workbench.hub.web import app as web_app
        events = []

        async def ws_handler(request):
            socket_ = web.WebSocketResponse()
            await socket_.prepare(request)
            request.app[web_app.WEBSOCKETS].add(socket_)
            async for _message in socket_:
                pass
            return socket_

        def routes(app):
            app.router.add_get('/ws/test', web_app.public(ws_handler))

        listener = self.listener(0)
        listener.routes = (routes,)
        await listener.start()
        port = listener.status()['port']
        services = listener.services
        issued = await asyncio.get_running_loop().run_in_executor(
            None, lambda: services.api_keys.issue('k1', 'editor', {'all': True}, None, now=5.0))
        await asyncio.get_running_loop().run_in_executor(None, lambda: services.api_keys.verify(issued.raw, 77.0))

        async def hook():
            try:
                await asyncio.open_connection('127.0.0.1', port)
                events.append('hook:port_open')
            except OSError:
                events.append('hook:port_closed')
        listener.stop_hooks.append(hook)
        async with ClientSession() as http:
            async with http.ws_connect(f'http://127.0.0.1:{port}/ws/test',
                                       headers={'Host': f'127.0.0.1:{port}'}) as ws:
                stopping = asyncio.create_task(listener.stop())
                message = await asyncio.wait_for(ws.receive(), 10)
                events.append(f'ws:{message.type.name}:{ws.close_code}')
                await asyncio.wait_for(stopping, 10)
        self.assertEqual(events, ['ws:CLOSE:1001', 'hook:port_closed'])
        self.assertIn(message.type, (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING))
        rows = sqlite3.connect(self.state / 'hub.sqlite3').execute(
            'SELECT last_used_at FROM api_keys WHERE key_id = ?', (issued.key_id,)).fetchall()
        self.assertEqual(rows, [(77.0,)])
        self.assertEqual(listener.status()['state'], 'stopped')


if __name__ == '__main__':
    unittest.main()
