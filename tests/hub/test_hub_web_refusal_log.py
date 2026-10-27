"""2026-10-27 사용자 요구: web refusals leave a rate-limited line in hub.log (stderr).

A user got ``421 misdirected_request`` and hub.log said nothing. Now a Host refusal (421) and an ``allow_from``
refusal (403) write one line each: at most one per distinct (reason, value) per 60 s and about 20 lines per minute
in total, with bounded in-memory counters. Seams: :class:`workbench.hub.web.refusals.RefusalLog` with an injected
clock and writer, and ``create_app(services)`` through aiohttp's ``TestServer`` (its ``services.refusals`` writes to
a list). Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp import ClientSession, DummyCookieJar, test_utils

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import auth  # noqa: E402
from workbench.hub.web.app import OriginPolicy, WebServices, create_app  # noqa: E402
from workbench.hub.web.refusals import RefusalLog  # noqa: E402


class RefusalLogUnitTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.lines: list[str] = []
        self.log = RefusalLog(clock=lambda: self.now[0], write=self.lines.append)

    def test_one_line_per_reason_and_value_per_window(self):
        self.assertTrue(self.log.note('host', 'a:1', 'first'))
        self.assertFalse(self.log.note('host', 'a:1', 'again'))
        self.assertTrue(self.log.note('host', 'b:1', 'other value'))
        self.assertTrue(self.log.note('peer', 'a:1', 'other reason'))
        self.now[0] += 59.0
        self.assertFalse(self.log.note('host', 'a:1', 'still inside the window'))
        self.now[0] += 1.5
        self.assertTrue(self.log.note('host', 'a:1', 'window over'))
        self.assertEqual(len(self.lines), 4)
        self.assertTrue(self.lines[0].startswith('first'))
        self.assertIn('2', self.lines[3])  # the two suppressed repeats are counted in the next line

    def test_the_total_cap_holds_and_memory_stays_bounded(self):
        for n in range(500):
            self.log.note('host', f'name-{n}', f'line {n}')
        self.assertEqual(len(self.lines), RefusalLog.TOTAL_PER_WINDOW)
        self.assertLessEqual(self.log.tracked(), RefusalLog.TOTAL_PER_WINDOW)
        self.now[0] += 61.0
        self.assertTrue(self.log.note('host', 'name-999', 'after a minute'))
        self.assertLessEqual(self.log.tracked(), RefusalLog.TOTAL_PER_WINDOW)
        self.assertIn('480', self.lines[-1])  # the refusals not logged are counted, never listed

    def test_sanitize_keeps_printable_ascii_only_and_cuts_at_100(self):
        from workbench.hub.web.refusals import sanitize
        self.assertEqual(sanitize('dgx-spark:8780'), "'dgx-spark:8780'")
        self.assertEqual(sanitize('a\x1b[31mb\r\nc\x00d한e'), "'a?[31mb??c?d?e'")
        self.assertEqual(sanitize('x' * 300), "'" + 'x' * 100 + "'")
        self.assertEqual(sanitize(None), '(none)')


class AppRefusalLogTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, *, allow_from: tuple = ()):
        temp = tempfile.TemporaryDirectory(prefix='wb-reflog-')
        self.addCleanup(temp.cleanup)
        state = Path(temp.name) / 'hub'
        passwords = auth.PasswordStore(state)
        passwords.set_password('correct horse battery')
        db = auth.HubDb.open(state)
        self.addCleanup(db.close)
        self.lines: list[str] = []
        self.now = [10.0]
        services = WebServices(passwords, db, OriginPolicy('127.0.0.1', allow_from=allow_from))
        services.refusals = RefusalLog(clock=lambda: self.now[0], write=self.lines.append)
        self.server = test_utils.TestServer(create_app(services), host='127.0.0.1', port=0)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.port = self.server.port
        self.http = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.http.close)

    async def get(self, host: str) -> int:
        async with self.http.get(f'http://127.0.0.1:{self.port}/login', headers={'Host': host},
                                 allow_redirects=False) as response:
            await response.read()
            return response.status

    async def test_a_refused_host_is_logged_once_with_value_listener_and_hint(self):
        await self.serve()
        for _ in range(5):
            self.assertEqual(await self.get(f'dgx-spark:{self.port}'), 421)
        self.assertEqual(len(self.lines), 1, self.lines)
        line = self.lines[0]
        self.assertTrue(line.startswith('[workbench] web: refused Host '), line)
        self.assertIn(f"'dgx-spark:{self.port}'", line)
        self.assertIn(f'on 127.0.0.1:{self.port}', line)
        self.assertIn('421 misdirected_request', line)
        self.assertIn('server_names', line)
        self.assertEqual(await self.get(f'127.0.0.1:{self.port}'), 200)
        self.assertEqual(len(self.lines), 1, 'an accepted Host is never logged')

    async def raw_get(self, host: bytes) -> bytes:
        """A raw HTTP/1.1 request (clients refuse to send such a Host); the status line."""
        import asyncio
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(b'GET /login HTTP/1.1\r\nHost: ' + host + b'\r\nConnection: close\r\n\r\n')
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return line

    async def test_a_host_with_non_printable_characters_is_logged_sanitized(self):
        await self.serve()
        self.assertIn(b' 421 ', await self.raw_get(b'evil\tna\xc3\xa9me'))
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertIn("'evil?na?me'", self.lines[0])  # é arrives as one character
        self.assertNotIn('\t', self.lines[0])
        self.assertTrue(self.lines[0].isascii())

    async def test_an_allow_from_refusal_names_the_peer_ip_once(self):
        await self.serve(allow_from=(ipaddress.ip_network('10.0.0.0/8'),))
        for _ in range(3):
            self.assertEqual(await self.get(f'127.0.0.1:{self.port}'), 403)
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertIn('refused client 127.0.0.1', self.lines[0])
        self.assertIn('allow_from', self.lines[0])
        self.assertIn(f'on 127.0.0.1:{self.port}', self.lines[0])


if __name__ == '__main__':
    unittest.main()
