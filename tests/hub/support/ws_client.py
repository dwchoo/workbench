"""A Python stand-in for one Board widget's WebSocket client, protocol v2 (web-board-ux SPEC s4 "WebSocket v2").

``Tab`` is one connection to one terminal: it connects to ``/ws/board/{terminal_id}`` with the session cookie and the
page's ``Origin``, sends the hello ``{"t": "hello", "v": 2, "csrf", "size": [rows, cols]}`` and reads control (text
JSON) and data (binary: the bare output bytes) frames. It does not reconnect by itself; tests drive every step.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time

import aiohttp

PROTOCOL_VERSION = 2


class Tab:
    """One connection. ``events`` collects every received control message (dicts) in order; ``output`` the data
    bytes and ``frames`` each data frame."""

    def __init__(self, base: str, cookie: str | None, terminal_id: str, *, origin: str | None = None,
                 headers: dict | None = None, cookie_name: str = 'wb_hub_session'):
        self.base = base
        self.terminal_id = terminal_id
        self.origin = base if origin is None else origin
        self.headers = dict(headers or {})
        if cookie is not None:
            self.headers['Cookie'] = f'{cookie_name}={cookie}'
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.events: list[dict] = []
        self.output = bytearray()
        self.frames: list[bytes] = []
        self.close_code: int | None = None
        self.closed_at: float | None = None

    async def connect(self) -> aiohttp.ClientWebSocketResponse:
        headers = dict(self.headers)
        if self.origin:
            headers['Origin'] = self.origin
        self.ws = await self.http.ws_connect(f'{self.base}/ws/board/{self.terminal_id}', headers=headers,
                                             autoping=True, max_msg_size=8 << 20)
        return self.ws

    async def hello(self, csrf, *, size=(30, 100), **extra) -> None:
        message = {'t': 'hello', 'v': PROTOCOL_VERSION, 'csrf': csrf, **extra}
        if size is not None:
            message['size'] = list(size)
        await self.ws.send_str(json.dumps(message))

    async def send(self, message: dict) -> None:
        await self.ws.send_str(json.dumps(message))

    async def send_input(self, payload: bytes) -> None:
        await self.ws.send_bytes(payload)

    async def _next(self, timeout: float):
        """One message, recorded; returns it (or None for the close)."""
        message = await asyncio.wait_for(self.ws.receive(), timeout)
        if message.type == aiohttp.WSMsgType.TEXT:
            event = json.loads(message.data)
            self.events.append(event)
            return event
        if message.type == aiohttp.WSMsgType.BINARY:
            self.frames.append(message.data)
            self.output += message.data
            return message.data
        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
            if self.close_code is None:
                self.close_code = self.ws.close_code
                self.closed_at = time.monotonic()
            return None
        return message

    async def receive_until(self, predicate, timeout: float = 10.0):
        """Read until ``predicate(item)`` holds for a received item (dict, bytes or None for the close)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f'timed out; events={self.events} close={self.close_code} '
                                     f'tail={bytes(self.output[-200:])!r}')
            item = await self._next(remaining)
            if predicate(item):
                return item
            if item is None:
                raise AssertionError(f'closed ({self.close_code}) before the expected item; events={self.events}')

    async def event(self, kind: str, timeout: float = 10.0) -> dict:
        return await self.receive_until(lambda item: isinstance(item, dict) and item.get('t') == kind, timeout)

    async def wait_output(self, needle: bytes, timeout: float = 10.0) -> None:
        if needle in self.output:
            return
        await self.receive_until(lambda item: needle in self.output, timeout)

    async def drain(self, quiet: float = 0.2) -> None:
        """Read (recording) until nothing arrives for ``quiet`` seconds or the connection closes."""
        while self.close_code is None:
            try:
                if await self._next(quiet) is None:
                    return
            except asyncio.TimeoutError:
                return

    async def wait_closed(self, timeout: float = 10.0) -> int | None:
        """Read (recording everything) until the server's close; returns the close code."""
        deadline = time.monotonic() + timeout
        while self.close_code is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f'not closed; events={self.events}')
            await self._next(remaining)
        return self.close_code

    async def attach(self, csrf: str, *, size=(30, 100)) -> dict:
        if self.ws is None:
            await self.connect()
        await self.hello(csrf, size=size)
        return await self.event('attached')

    def kill(self) -> None:
        """Browser killed: the TCP connection ends without a close frame."""
        sock = self.ws.get_extra_info('socket') if self.ws is not None else None
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            try:
                await asyncio.wait_for(self.ws.close(), 3)
            except (asyncio.TimeoutError, OSError, aiohttp.ClientError):
                pass
        await self.http.close()
