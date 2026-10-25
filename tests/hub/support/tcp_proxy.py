"""A loopback TCP proxy for connection-lifetime tests (TI-17, seam T5).

``pause()`` stops forwarding both ways and holds what arrives (a network cut: neither side sees an end, both see
silence); ``resume()`` delivers what was held and forwards again. ``reset()`` ends every connection with a TCP RST
on both sides. One proxy forwards to one upstream ``(host, port)``.
"""
from __future__ import annotations

import asyncio
import socket
import struct


def _rst(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info('socket')
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        except OSError:
            pass
    writer.transport.abort()


class TcpProxy:
    def __init__(self, upstream_host: str, upstream_port: int):
        self.upstream = (upstream_host, upstream_port)
        self.flowing = asyncio.Event()
        self.flowing.set()
        self.server: asyncio.base_events.Server | None = None
        self.port: int | None = None
        self.pairs: list[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = []
        self.tasks: set[asyncio.Task] = set()

    async def start(self) -> 'TcpProxy':
        self.server = await asyncio.start_server(self._accept, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    async def _accept(self, client_reader, client_writer) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self.upstream)
        except OSError:
            client_writer.close()
            return
        self.pairs.append((client_writer, upstream_writer))
        for reader, writer in ((client_reader, upstream_writer), (upstream_reader, client_writer)):
            task = asyncio.create_task(self._pump(reader, writer))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await self.flowing.wait()
                data = await reader.read(65536)
                if not data:
                    break
                await self.flowing.wait()  # a pause while reading holds this chunk too
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    def pause(self) -> None:
        self.flowing.clear()

    def resume(self) -> None:
        self.flowing.set()

    def reset(self) -> None:
        for client, upstream in self.pairs:
            _rst(client)
            _rst(upstream)
        self.pairs.clear()

    async def close(self) -> None:
        self.resume()
        for client, upstream in self.pairs:
            client.transport.abort()
            upstream.transport.abort()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
