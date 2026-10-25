"""A Python stand-in for the Board page's WebSocket client (Task Inbox SPEC "WebSocket terminal 프로토콜").

``Tab`` is one browser tab: it connects to ``/ws/board`` with the session cookie and the page's ``Origin``, sends
the hello, and reads control (text JSON) and data (binary ``channel | epoch(uint32 BE) | payload``) frames. It does
not reconnect by itself (TI-18's client rules); tests drive every step.
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
import time

import aiohttp

CHANNEL_IDS = {'agent': 0, 'shell': 1}
CHANNEL_NAMES = {value: key for key, value in CHANNEL_IDS.items()}


def frame(channel: str | int, epoch: int, payload: bytes) -> bytes:
    channel_id = CHANNEL_IDS[channel] if isinstance(channel, str) else channel
    return bytes([channel_id]) + struct.pack('>I', epoch) + payload


def parse_frame(data: bytes) -> tuple[str, int, bytes]:
    return CHANNEL_NAMES[data[0]], struct.unpack('>I', data[1:5])[0], data[5:]


class Tab:
    """One tab's connection. ``events`` collects every received control message (dicts) in order; ``output``
    collects the data bytes per channel."""

    def __init__(self, base: str, cookie: str | None, *, origin: str | None = None, headers: dict | None = None,
                 cookie_name: str = 'wb_hub_session'):
        self.base = base
        self.origin = base if origin is None else origin
        self.headers = dict(headers or {})
        if cookie is not None:
            self.headers['Cookie'] = f'{cookie_name}={cookie}'
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.events: list[dict] = []
        self.output: dict[str, bytearray] = {'agent': bytearray(), 'shell': bytearray()}
        self.frames: list[tuple[str, int, bytes]] = []
        self.close_code: int | None = None
        self.closed_at: float | None = None

    async def connect(self) -> aiohttp.ClientWebSocketResponse:
        headers = dict(self.headers)
        if self.origin:
            headers['Origin'] = self.origin
        self.ws = await self.http.ws_connect(self.base + '/ws/board', headers=headers, autoping=True,
                                             max_msg_size=8 << 20)
        return self.ws

    async def hello(self, csrf: str, *, resume: dict | None = None, sizes: dict | None = None, **extra) -> None:
        message = {'t': 'hello', 'v': 1, 'csrf': csrf, 'resume': resume,
                   'sizes': sizes if sizes is not None else {'agent': [30, 100], 'shell': [30, 100]}, **extra}
        await self.ws.send_str(json.dumps(message))

    async def send(self, message: dict) -> None:
        await self.ws.send_str(json.dumps(message))

    async def send_input(self, channel: str | int, epoch: int, payload: bytes) -> None:
        await self.ws.send_bytes(frame(channel, epoch, payload))

    async def _next(self, timeout: float):
        """One message, recorded; returns it (or the close)."""
        message = await asyncio.wait_for(self.ws.receive(), timeout)
        if message.type == aiohttp.WSMsgType.TEXT:
            event = json.loads(message.data)
            self.events.append(event)
            return event
        if message.type == aiohttp.WSMsgType.BINARY:
            channel, epoch, payload = parse_frame(message.data)
            self.frames.append((channel, epoch, payload))
            self.output[channel] += payload
            return (channel, epoch, payload)
        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
            if self.close_code is None:
                self.close_code = self.ws.close_code
                self.closed_at = time.monotonic()
            return None
        return message

    async def receive_until(self, predicate, timeout: float = 10.0):
        """Read until ``predicate(item)`` holds for a received item (dict, frame tuple or None for close)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f'timed out; events={self.events} close={self.close_code}')
            item = await self._next(remaining)
            if predicate(item):
                return item
            if item is None:
                raise AssertionError(f'closed ({self.close_code}) before the expected item; events={self.events}')

    async def event(self, kind: str, timeout: float = 10.0) -> dict:
        return await self.receive_until(lambda item: isinstance(item, dict) and item.get('t') == kind, timeout)

    async def wait_output(self, channel: str, needle: bytes, timeout: float = 10.0) -> None:
        if needle in self.output[channel]:
            return
        await self.receive_until(lambda item: needle in self.output[channel], timeout)

    async def wait_closed(self, timeout: float = 10.0) -> int | None:
        """Read (recording everything) until the server's close; returns the close code."""
        deadline = time.monotonic() + timeout
        while self.close_code is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f'not closed; events={self.events}')
            await self._next(remaining)
        return self.close_code

    async def attach(self, csrf: str, *, resume: dict | None = None, sizes: dict | None = None) -> dict:
        if self.ws is None:
            await self.connect()
        await self.hello(csrf, resume=resume, sizes=sizes)
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
