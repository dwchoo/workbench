"""Unsigned UID refusal through public UiClient and an actual owned socket.

Linux socket.h fixes credential order as pid, uid, gid. Only credential bytes are
substituted: no process_state success mock and no actual privilege-change claim.
"""
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.backend.client import ClientError, UiClient


class IndependentPeerCredentialTests(unittest.TestCase):
    def test_high_bit_foreign_uid_with_matching_gid_is_refused_before_hello(self):
        actual_socket = socket.socket
        received = []
        failures = []
        with tempfile.TemporaryDirectory(prefix='wi31-peer-') as temp:
            endpoint = Path(temp) / 'ui.sock'
            server = actual_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(endpoint)); server.listen(); server.settimeout(3)
            def serve():
                try:
                    connection, _ = server.accept()
                    with connection:
                        connection.settimeout(3)
                        received.append(connection.recv(4096))
                except BaseException as exc:
                    failures.append(exc)
            thread = threading.Thread(target=serve)
            thread.start()
            class CredentialBytesOnly:
                def __init__(self, *args, **kwargs):
                    self.real = actual_socket(*args, **kwargs)
                def __getattr__(self, key):
                    return getattr(self.real, key)
                def getsockopt(self, level, option, *args):
                    if (level, option) == (socket.SOL_SOCKET, socket.SO_PEERCRED):
                        return struct.pack('iII', os.getpid(), 2147484648, os.geteuid())
                    return self.real.getsockopt(level, option, *args)
            try:
                with patch('workbench.backend.client.socket.socket', CredentialBytesOnly):
                    with self.assertRaisesRegex(ClientError, 'UID mismatch'):
                        UiClient(endpoint, timeout=.3)
            finally:
                thread.join(timeout=4)
                server.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(received, [b''])


if __name__ == '__main__':
    unittest.main()
