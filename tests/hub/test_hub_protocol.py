"""hub_v1 control socket protocol against a real ``_hub --foreground`` (HB-01, seam S2).

The test speaks raw newline-delimited JSON over the Hub's socket; expected frames come
from the SPEC "hub_v1 제어 socket 프로토콜" table. Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

import json
import os
import socket
import time
import unittest

from test_hub_process import HubSandbox, flock_holder, proc_start_ticks, wait_until


class Line:
    """A blocking raw hub_v1 connection with a test-side line buffer."""

    def __init__(self, path: str, timeout: float = 5.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buffer = b''

    def send(self, value) -> None:
        self.sock.sendall(json.dumps(value).encode() + b'\n')

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self) -> dict | None:
        """The next frame, or None on EOF."""
        while b'\n' not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def closed(self, timeout: float = 5.0) -> bool:
        """True when the server closes the connection (EOF/reset) within ``timeout``."""
        self.sock.settimeout(timeout)
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return True
        except (ConnectionResetError, BrokenPipeError):
            return True
        except socket.timeout:
            return False

    def hello(self) -> dict | None:
        self.send({'v': 1, 'type': 'hello', 'client': 'test'})
        return self.recv()

    def request(self, request_id: str, op: str, **fields) -> dict | None:
        self.send({'v': 1, 'id': request_id, 'type': op, **fields})
        return self.recv()

    def close(self) -> None:
        self.sock.close()


class HubProtocolTests(unittest.TestCase):
    def setUp(self):
        self.box = HubSandbox(self)

    def connect(self, hub_record: dict | None = None, timeout: float = 5.0) -> Line:
        record = hub_record or self.box.record()
        line = Line(record['socket'], timeout)
        self.addCleanup(line.close)
        return line

    def test_hello_is_welcomed_with_the_hub_identity(self):
        hub = self.box.start_hub()
        welcome = self.connect().hello()
        self.assertEqual(welcome['v'], 1)
        self.assertEqual(welcome['type'], 'welcome')
        self.assertEqual(welcome['hub']['pid'], hub.pid)
        self.assertEqual(welcome['hub']['start_ticks'], proc_start_ticks(hub.pid))
        self.assertEqual(welcome['hub']['mode'], 'foreground')
        self.assertEqual(set(welcome['hub']), {'pid', 'start_ticks', 'version', 'started_at', 'mode'})

    def test_unsupported_hello_version_is_rejected_and_closed(self):
        self.box.start_hub()
        line = self.connect()
        line.send({'v': 2, 'type': 'hello', 'client': 'test'})
        self.assertEqual(line.recv(), {'v': 1, 'type': 'reject', 'reason': 'version_mismatch'})
        self.assertTrue(line.closed())

    def test_status_reports_the_hub_record_registry_and_unavailable_web(self):
        hub = self.box.start_hub()
        record = self.box.record()
        line = self.connect()
        line.hello()
        reply = line.request('s1', 'status')
        self.assertEqual({key: reply[key] for key in ('v', 'id', 'type', 'ok')},
                         {'v': 1, 'id': 's1', 'type': 'result', 'ok': True})
        self.assertEqual(reply['hub'], record)
        self.assertEqual(reply['hub']['pid'], hub.pid)
        self.assertEqual(reply['registry'], {'state': 'ok', 'count': 0,
                                             'path': str(self.box.state / 'registry.json')})
        self.assertEqual(reply['web'], {'state': 'not_available'})
        self.assertIsNone(reply['board_agent'])

    def assert_stopped_and_clean(self, hub, record: dict) -> None:
        self.assertEqual(hub.wait(timeout=10), 0)
        self.assertIsNone(flock_holder(self.box.lock) if self.box.lock.exists() else None)
        self.assertFalse(os.path.lexists(record['socket']))
        self.assertFalse(os.path.lexists(self.box.record_path))

    def test_stop_request_then_confirm_stops_the_hub_and_cleans_up(self):
        hub = self.box.start_hub()
        record = self.box.record()
        line = self.connect()
        line.hello()
        offer = line.request('r1', 'stop_request')
        self.assertTrue(offer['ok'])
        self.assertEqual(offer['active'], [])
        self.assertEqual(offer['expires_in'], 60)
        self.assertIsInstance(offer['token'], str)
        self.assertTrue(offer['token'])
        reply = line.request('r2', 'stop_confirm', token=offer['token'])
        self.assertEqual({key: reply[key] for key in ('id', 'type', 'ok', 'stopping')},
                         {'id': 'r2', 'type': 'result', 'ok': True, 'stopping': True})
        self.assert_stopped_and_clean(hub, record)

    def test_wrong_stop_token_is_refused_and_the_hub_keeps_running(self):
        hub = self.box.start_hub()
        line = self.connect()
        line.hello()
        offer = line.request('r1', 'stop_request')
        reply = line.request('r2', 'stop_confirm', token=offer['token'] + 'x')
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['reason'], 'stop_token_mismatch')
        self.assertIsInstance(reply['detail'], str)
        time.sleep(0.3)
        self.assertIsNone(hub.poll())
        self.assertTrue(line.request('r3', 'status')['ok'])
        self.assertEqual(flock_holder(self.box.lock), hub.pid)

    def test_stop_confirm_without_a_request_is_refused(self):
        hub = self.box.start_hub()
        line = self.connect()
        line.hello()
        reply = line.request('r1', 'stop_confirm', token='0' * 32)
        self.assertEqual((reply['ok'], reply['reason']), (False, 'stop_token_mismatch'))
        self.assertIsNone(hub.poll())

    def test_expired_stop_token_is_refused(self):
        hub = self.box.start_hub('--stop-token-ttl', '1')
        line = self.connect()
        line.hello()
        offer = line.request('r1', 'stop_request')
        self.assertEqual(offer['expires_in'], 1)
        time.sleep(1.5)
        reply = line.request('r2', 'stop_confirm', token=offer['token'])
        self.assertEqual((reply['ok'], reply['reason']), (False, 'stop_token_mismatch'))
        self.assertIsNone(hub.poll())


MIB = 1 << 20


class HubFramingTests(unittest.TestCase):
    def setUp(self):
        self.box = HubSandbox(self)

    def connect(self, timeout: float = 5.0) -> Line:
        line = Line(self.box.record()['socket'], timeout)
        self.addCleanup(line.close)
        return line

    def test_a_line_of_exactly_one_mebibyte_is_served(self):
        self.box.start_hub()
        line = self.connect()
        line.hello()
        head = b'{"v":1,"id":"big","type":"status","pad":"'
        tail = b'"}\n'
        line.send_raw(head + b'a' * (MIB - len(head) - len(tail)) + tail)
        reply = line.recv()
        self.assertEqual((reply['id'], reply['ok']), ('big', True))

    def test_a_line_over_one_mebibyte_is_rejected_and_closed(self):
        hub = self.box.start_hub()
        line = self.connect()
        line.hello()
        try:
            line.send_raw(b'{"v":1,"id":"big","type":"status","pad":"' + b'a' * MIB)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.assertEqual(line.recv(), {'v': 1, 'type': 'reject', 'reason': 'protocol_error'})
        self.assertTrue(line.closed())
        self.assertIsNone(hub.poll())

    def test_non_json_line_is_rejected_and_closed(self):
        self.box.start_hub()
        for greet in (False, True):
            line = self.connect()
            if greet:
                line.hello()
            line.send_raw(b'not json\n')
            self.assertEqual(line.recv(), {'v': 1, 'type': 'reject', 'reason': 'protocol_error'})
            self.assertTrue(line.closed())

    def test_unknown_and_reserved_ops_are_unsupported_type(self):
        hub = self.box.start_hub()
        line = self.connect()
        line.hello()
        for index, op in enumerate(('frobnicate', 'power_on', 'power_off_request', 'power_off_confirm',
                                    'pause', 'resume', 'confirm_boot')):
            reply = line.request(f'u{index}', op)
            self.assertEqual((reply['id'], reply['type'], reply['ok'], reply['reason']),
                             (f'u{index}', 'result', False, 'unsupported_type'), op)
        self.assertTrue(line.request('after', 'status')['ok'])
        self.assertIsNone(hub.poll())

    def test_seventeenth_connection_is_refused(self):
        self.box.start_hub()
        open_lines = [self.connect() for _ in range(16)]
        for line in open_lines:
            self.assertEqual(line.hello()['type'], 'welcome')
        extra = self.connect()
        try:
            extra.send({'v': 1, 'type': 'hello', 'client': 'test'})
            reply = extra.recv()
        except (BrokenPipeError, ConnectionResetError):
            reply = None
        self.assertNotEqual((reply or {}).get('type'), 'welcome')
        self.assertTrue(reply is None or reply == {'v': 1, 'type': 'reject', 'reason': 'connection_limit'})
        open_lines[0].close()
        time.sleep(0.2)
        self.assertTrue(wait_until(lambda: self.connect().hello()['type'] == 'welcome', 3))

    def test_idle_connection_is_closed_but_an_active_one_is_kept(self):
        self.box.start_hub('--idle-timeout', '1')
        idle = self.connect()
        busy = self.connect()
        idle.hello()
        busy.hello()
        for index in range(6):  # 1.8 s of requests, each under the idle limit
            time.sleep(0.3)
            self.assertTrue(busy.request(f'b{index}', 'status')['ok'])
        self.assertTrue(idle.closed(timeout=1))
        self.assertTrue(busy.request('last', 'status')['ok'])


if __name__ == '__main__':
    unittest.main()
