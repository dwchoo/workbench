"""2026-10-27 사용자 결정: ``[web] bind`` takes one IP or a list of IPs, ``[web] allow_from`` limits client IPs.

Seams: the ``hub.toml`` parse API (``workbench.hub.config``), ``WebListener`` on a real loop with real loopback
sockets (127.0.0.0/8, ``::1`` when the host has it), and ``create_app(services)`` through aiohttp's
``TestServer``. Expected values come from the user decision (config items, 403 without body or cookie, URLs per
address) and from ``/proc``-free socket probes. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
import socket
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from workbench.hub import config  # noqa: E402


def parse(text: str) -> config.HubConfig:
    return config.parse_config(text.encode('utf-8'))


class BindListParseTests(unittest.TestCase):
    def assert_invalid(self, text: str, item: str) -> None:
        with self.assertRaises(config.ConfigInvalid) as caught:
            parse(text)
        self.assertEqual(caught.exception.reason, f'config_invalid:{item}')

    def test_one_string_is_still_one_address(self):
        web = parse('[web]\nbind = "127.0.0.2"\n').web
        self.assertEqual(web.bind, '127.0.0.2')
        self.assertEqual(web.addresses, ('127.0.0.2',))
        self.assertEqual(config.HubConfig().web.addresses, ('127.0.0.1',))

    def test_a_list_gives_every_address_in_order_and_bind_is_the_first(self):
        web = parse('[web]\nbind = ["127.0.0.2", "::1", "0.0.0.0"]\n').web
        self.assertEqual(web.addresses, ('127.0.0.2', '::1', '0.0.0.0'))
        self.assertEqual(web.bind, '127.0.0.2')
        self.assertEqual(parse('[web]\nbind = ["10.8.0.2"]\n').web.addresses, ('10.8.0.2',))

    def test_bad_lists_are_config_invalid_web_bind(self):
        for value in ('[]', '["127.0.0.1", "127.0.0.1"]', '["::1", "0:0:0:0:0:0:0:1"]', '["127.0.0.1", 1]',
                      '["127.0.0.1", "localhost"]', '["box.vpn"]', '[["127.0.0.1"]]', '["fe80::1%eth0"]',
                      '[""]', '{a = 1}', '1', 'true'):
            with self.subTest(value=value):
                self.assert_invalid(f'[web]\nbind = {value}\n', 'web.bind')

    def test_too_many_addresses_are_invalid(self):
        many = ', '.join(f'"127.0.0.{n}"' for n in range(1, config.BIND_MAX + 2))
        self.assert_invalid(f'[web]\nbind = [{many}]\n', 'web.bind')
        fine = ', '.join(f'"127.0.0.{n}"' for n in range(1, config.BIND_MAX + 1))
        self.assertEqual(len(parse(f'[web]\nbind = [{fine}]\n').web.addresses), config.BIND_MAX)


class AllowFromParseTests(unittest.TestCase):
    def assert_invalid(self, text: str) -> None:
        with self.assertRaises(config.ConfigInvalid) as caught:
            parse(text)
        self.assertEqual(caught.exception.reason, 'config_invalid:web.allow_from')

    def test_absent_and_empty_mean_no_filter(self):
        self.assertEqual(config.HubConfig().web.allow_from, ())
        self.assertEqual(parse('[web]\n').web.allow_from, ())
        self.assertEqual(parse('[web]\nallow_from = []\n').web.allow_from, ())

    def test_addresses_and_networks_of_both_families_are_read_as_networks(self):
        web = parse('[web]\nallow_from = ["100.64.0.0/10", "192.168.10.0/24", "127.0.0.1", "::1", "fd00::/8"]\n').web
        self.assertEqual(web.allow_from, tuple(ipaddress.ip_network(text) for text in (
            '100.64.0.0/10', '192.168.10.0/24', '127.0.0.1/32', '::1/128', 'fd00::/8')))

    def test_host_bits_set_are_refused_not_widened(self):
        # 2026-10-27 decision: "192.168.10.5/24" is ambiguous (one host or the /24?); strict rejection.
        self.assert_invalid('[web]\nallow_from = ["192.168.10.5/24"]\n')
        self.assert_invalid('[web]\nallow_from = ["fd00::1/8"]\n')

    def test_bad_entries_are_config_invalid(self):
        for value in ('"127.0.0.1"', '["localhost"]', '["box.vpn"]', '[""]', '[1]', '["10.0.0.0/33"]',
                      '["fe80::1%eth0"]', '["::ffff:10.0.0.1"]', '["::ffff:0:0/96"]', '[["127.0.0.1"]]',
                      '["127.0.0.1/"]', '["*"]'):
            with self.subTest(value=value):
                self.assert_invalid(f'[web]\nallow_from = {value}\n')

    def test_too_many_entries_are_invalid_and_duplicates_collapse(self):
        many = ', '.join(f'"10.0.{n // 256}.{n % 256}"' for n in range(config.ALLOW_FROM_MAX + 1))
        self.assert_invalid(f'[web]\nallow_from = [{many}]\n')
        self.assertEqual(parse('[web]\nallow_from = ["127.0.0.1", "127.0.0.1/32"]\n').web.allow_from,
                         (ipaddress.ip_network('127.0.0.1/32'),))


class SelfSignedSanTests(unittest.TestCase):
    """``wb hub tls --self-signed`` names every listening address, so HTTPS works on each of them."""

    def test_every_bind_address_is_a_san(self):
        from workbench.hub import tls
        web = parse('[web]\nbind = ["127.0.0.1", "100.101.102.103", "fd7a::5"]\nserver_names = ["box.vpn"]\n').web
        entries = tls.san_entries(web, discover=lambda: (['192.168.1.9'], ['host']))
        self.assertEqual(entries, ('IP:127.0.0.1', 'IP:::1', 'IP:100.101.102.103', 'IP:fd7a::5',
                                   'DNS:localhost', 'DNS:box.vpn'))

    def test_a_wildcard_in_the_list_adds_the_discovered_host_addresses(self):
        from workbench.hub import tls
        web = parse('[web]\nbind = ["100.101.102.103", "0.0.0.0"]\n').web
        entries = tls.san_entries(web, discover=lambda: (['192.168.1.9'], ['host']))
        self.assertEqual(entries, ('IP:127.0.0.1', 'IP:::1', 'IP:100.101.102.103', 'IP:192.168.1.9',
                                   'DNS:localhost', 'DNS:host'))


def ipv6_loopback() -> bool:
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind(('::1', 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def answers(host: str, port: int, header_host: str | None = None) -> int:
    """HTTP status of ``GET /login`` at ``host:port`` (``Host`` defaults to that address)."""
    import http.client
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request('GET', '/login', headers={'Host': header_host or config_url_host(host, port)})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def config_url_host(host: str, port: int) -> str:
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


def refused(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    probe.settimeout(5)
    try:
        probe.connect((host, port))
    except ConnectionRefusedError:
        return True
    finally:
        probe.close()
    return False


class ListenerCase(unittest.IsolatedAsyncioTestCase):
    """``WebListener`` on a real loop and real loopback sockets."""

    async def asyncSetUp(self):
        from workbench.hub import auth
        from workbench.hub.web.listener import WebListener
        self.temp = tempfile.TemporaryDirectory(prefix='wb-mb-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'hub'
        auth.PasswordStore(self.state).set_password('correct horse battery')
        self.WebListener = WebListener

    def listener(self, web_lines: str, **options):
        loaded = parse(f'[web]\nenabled = true\n{web_lines}\n')
        decision = config.decide_tls(loaded.web, Path('/nonexistent/c'), Path('/nonexistent/k'))
        web = self.WebListener(self.state, config=loaded, config_error=None, tls=decision, **options)
        self.addAsyncCleanup(web.stop)
        return web


class MultiBindListenerTests(ListenerCase):
    async def test_every_address_listens_on_one_port_and_status_lists_every_url(self):
        addresses = ['127.0.0.1', '127.0.0.2'] + (['::1'] if ipv6_loopback() else [])
        listed = ', '.join(f'"{address}"' for address in addresses)
        web = self.listener(f'bind = [{listed}]\nport = 0')
        await web.start()
        status = web.status()
        self.assertEqual(status['state'], 'listening', status)
        port = status['port']
        self.assertNotEqual(port, 0)
        urls = [f'http://{config_url_host(address, port)}' for address in addresses]
        self.assertEqual(status['urls'], urls)
        self.assertEqual(status['url'], urls[0])  # backward compatible: the first one
        self.assertEqual(status['bind'], '127.0.0.1')
        self.assertEqual(status['binds'], addresses)
        self.assertFalse(status['insecure_warning'])
        for address in addresses:
            with self.subTest(address=address):
                self.assertEqual(await self.get_status(address, port), 200)
        # Host check: every bind IP is accepted on any of the addresses; an unlisted IP is not.
        self.assertEqual(await self.get_status('127.0.0.1', port, f'127.0.0.2:{port}'), 200)
        self.assertEqual(await self.get_status('127.0.0.2', port, f'127.0.0.1:{port}'), 200)
        self.assertEqual(await self.get_status('127.0.0.2', port, f'127.0.0.3:{port}'), 421)

    async def get_status(self, host: str, port: int, header_host: str | None = None) -> int:
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(None, answers, host, port, header_host)

    async def test_a_lan_address_in_the_list_turns_the_plain_http_warning_on(self):
        from workbench.hub.config import insecure_warning
        self.assertFalse(insecure_warning(('127.0.0.1', '::1'), tls=False))
        self.assertTrue(insecure_warning(('127.0.0.1', '10.8.0.2'), tls=False))
        self.assertTrue(insecure_warning(('127.0.0.1', '0.0.0.0'), tls=False))
        self.assertFalse(insecure_warning(('127.0.0.1', '10.8.0.2'), tls=True))


class PartialBindFailureTests(ListenerCase):
    async def test_one_taken_address_closes_every_address_and_names_the_failing_one(self):
        holder = socket.socket()
        holder.bind(('127.0.0.2', 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        port = holder.getsockname()[1]
        web = self.listener(f'bind = ["127.0.0.1", "127.0.0.2"]\nport = {port}')
        await web.start()
        status = web.status()
        self.assertEqual(status['state'], 'bind_failed', status)
        self.assertEqual(status['detail'], 'EADDRINUSE')
        self.assertEqual(status['address'], f'127.0.0.2:{port}')
        self.assertTrue(refused('127.0.0.1', port), 'the first address must not stay listening')
        from workbench.hub.cli import web_status_lines
        self.assertEqual(web_status_lines(status), [f'web: not_listening (bind_failed:EADDRINUSE {"127.0.0.2"}:{port})'])

    async def test_port_zero_gives_every_address_the_same_port(self):
        web = self.listener('bind = ["127.0.0.1", "127.0.0.2", "127.0.0.3"]\nport = 0')
        await web.start()
        status = web.status()
        self.assertEqual(status['state'], 'listening', status)
        self.assertEqual(len({url.rsplit(':', 1)[1] for url in status['urls']}), 1)


if __name__ == '__main__':
    unittest.main()
