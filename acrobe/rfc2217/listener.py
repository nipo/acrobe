"""TCP listener serving one SerialPort over RFC 2217."""

import asyncio
import logging

from ..protocol.serial import SerialPort
from ..protocol.pipe import Pipe, Read, Write
from ..protocol.telnet import TelnetPipe
from .server import ComPortServer


class _StreamPipe(Pipe):
    """Adapts an asyncio.StreamReader/StreamWriter pair to the Pipe
    interface."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, name: str = "stream"):
        super().__init__(name)
        self._reader = reader
        self._writer = writer

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Write):
                try:
                    self._writer.write(op.data)
                    await self._writer.drain()
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                    continue
                if not future.done():
                    future.set_result(None)
            elif isinstance(op, Read):
                asyncio.create_task(self._read_task(op.size, future))
            else:
                if not future.done():
                    future.set_exception(TypeError(
                        f"_StreamPipe: unsupported op {type(op).__name__}"))

    _STREAM_CHUNK = 65536

    async def _read_task(self, size, future):
        try:
            if size is None:
                data = await self._reader.read(self._STREAM_CHUNK)
                if not data:
                    raise EOFError("_StreamPipe: connection closed")
            else:
                data = await self._reader.readexactly(size)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            return
        if not future.done():
            future.set_result(data)


class Rfc2217Listener:
    """Accept RFC 2217 clients and bridge them to a local SerialPort.

    Single-client by default: when a client is active, incoming
    connections are closed immediately. Set on_conflict='evict' to
    instead disconnect the existing client and accept the new one
    (not implemented yet — raises NotImplementedError).
    """

    def __init__(self, serial: SerialPort, *,
                 host: str = "0.0.0.0", port: int = 2217,
                 on_conflict: str = "reject",
                 logger: logging.Logger | None = None):
        if on_conflict not in ("reject", "evict"):
            raise ValueError(f"Bad on_conflict {on_conflict!r}")
        if on_conflict == "evict":
            raise NotImplementedError("on_conflict='evict' not implemented")
        self._serial = serial
        self._host = host
        self._port = port
        self._on_conflict = on_conflict
        self.logger = logger or logging.getLogger("rfc2217")
        self._server: asyncio.base_events.Server | None = None
        self._active: tuple[asyncio.StreamWriter, ComPortServer] | None = None

    async def serve_forever(self):
        self._server = await asyncio.start_server(
            self._on_connect, self._host, self._port)
        self.logger.info("Serving %s on %s:%d",
                         self._serial.fqdn, self._host, self._port)
        async with self._server:
            await self._server.serve_forever()

    async def _on_connect(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        if self._active is not None:
            self.logger.info("Rejecting %s: already serving a client", peer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        self.logger.info("Accepted %s", peer)
        pipe = _StreamPipe(reader, writer)
        telnet = TelnetPipe(pipe, logger=self.logger)
        comport = ComPortServer(telnet, self._serial)
        self._active = (writer, comport)
        try:
            await comport.start()
            # Wait until either pump exits (connection closed)
            done, pending = await asyncio.wait(
                comport._tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        except Exception as e:
            self.logger.exception("Connection %s errored: %r", peer, e)
        finally:
            await comport.stop()
            await telnet.close()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self._active = None
            self.logger.info("Closed %s", peer)
