"""asyncio TCP server hosting a GDB Remote Serial Protocol Responder.

One Session per TCP connection. Each session owns a fresh
Responder bound to the shared Debuggable / Loadable. The server
loop reads bytes, accumulates them into packets, hands packets to
the Responder, and writes responses. Ack-mode and Ctrl-C (0x03)
interrupts are handled at the Session layer.
"""

from __future__ import annotations

import asyncio
import logging

from . import message
from .protocol import Responder


class Session:
    """One client connection. Owns the parser state and the
    Responder. Cancellation-safe: closing the writer cleanly
    surfaces as a SocketClosed-equivalent at the end of `serve`."""

    INTERRUPT = 0x03  # Ctrl-C, sent outside packets

    def __init__(self, reader, writer, responder, *, logger=None):
        self.reader = reader
        self.writer = writer
        self.responder = responder
        self.logger = logger or logging.getLogger("acrobe.gdb")
        self.buffer = bytearray()
        peer = writer.get_extra_info("peername")
        self.peer_name = f"{peer[0]}:{peer[1]}" if peer else "?"

    async def serve(self) -> None:
        self.logger.info("session start: %s", self.peer_name)
        try:
            while True:
                chunk = await self.reader.read(4096)
                if not chunk:
                    break
                self.buffer.extend(chunk)
                await self.__drain_buffer()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
            self.logger.info("session end: %s", self.peer_name)

    async def __drain_buffer(self) -> None:
        """Parse + dispatch as many complete packets as the buffer
        currently holds. Interrupt bytes and bare +/- ack bytes
        are handled inline."""
        while self.buffer:
            b = self.buffer[0]
            if b == self.INTERRUPT:
                self.buffer.pop(0)
                response = await self.responder.handle_interrupt()
                if response is not None:
                    await self.__send_response(response)
                continue
            if b == 0x2B:  # '+', client ACK
                self.buffer.pop(0)
                continue
            if b == 0x2D:  # '-', client NACK
                self.buffer.pop(0)
                # Spec says retransmit last packet; we don't keep one
                # because no-ack mode is the common case. Logging only.
                self.logger.debug("got NACK from %s", self.peer_name)
                continue
            if b != 0x24:  # '$'
                # Resynchronise on $.
                self.buffer.pop(0)
                continue
            end = self.buffer.find(b"#")
            if end < 0 or end + 2 >= len(self.buffer):
                return  # need more bytes
            packet = bytes(self.buffer[:end + 3])
            del self.buffer[:end + 3]
            payload = message.Packet.parse(packet)
            if payload is None:
                if self.responder.packet_ack:
                    self.writer.write(b"-")
                    await self.writer.drain()
                self.logger.warning("bad packet from %s: %s",
                                    self.peer_name, packet)
                continue
            if self.responder.packet_ack:
                self.writer.write(b"+")
                await self.writer.drain()
            response = await self.responder.handle_packet(payload)
            if response is not None:
                await self.__send_response(response)
            # QStartNoAckMode flips the mode after the ack is sent.
            if self.responder.no_ack_mode_requested:
                self.responder.packet_ack = False
                self.responder.no_ack_mode_requested = False

    async def __send_response(self, payload: bytes) -> None:
        framed = message.frame(payload)
        self.writer.write(framed)
        await self.writer.drain()


class GdbServer:
    """asyncio TCP server. Constructs a fresh Responder per
    connection so state (current thread, flash image, ack mode)
    stays per-session while Debuggable + Loadable are shared."""

    def __init__(self, debuggable, loadable=None, *,
                 host: str = "localhost", port: int = 3333,
                 logger=None):
        self.debuggable = debuggable
        self.loadable = loadable
        self.host = host
        self.port = port
        self.logger = logger or logging.getLogger("acrobe.gdb.server")
        self.__server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.__server = await asyncio.start_server(
            self.__on_connect, self.host, self.port)
        sockets = self.__server.sockets or ()
        for sock in sockets:
            self.logger.info("listening on %s", sock.getsockname())

    async def serve_forever(self) -> None:
        if self.__server is None:
            await self.start()
        async with self.__server:
            await self.__server.serve_forever()

    async def close(self) -> None:
        if self.__server is None:
            return
        self.__server.close()
        await self.__server.wait_closed()
        self.__server = None

    async def __on_connect(self, reader, writer) -> None:
        responder = Responder(self.debuggable, self.loadable)
        session = Session(reader, writer, responder, logger=self.logger)
        await session.serve()
