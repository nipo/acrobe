"""TCP listener implementing the etherlink 5-socket handshake.

Single-client at a time: while one session holds the data sockets, new
incoming connections receive ``SERVER_BUSY\\0`` and close. After the
session ends (DISCONNECT or socket loss), a fresh client may connect.

The handshake follows Intel's reference verbatim so jtagd plugs in
unchanged:

1. Server accepts CTRL → sends NUL-terminated welcome banner with a
   server-chosen ``HANDLE`` integer.
2. Client replies ``"Control HANDLE=<int>\\0"``.
3. Server replies ``"READY\\0"``.
4. Server accepts each of MGMT, MGMT_RSP, H2T, T2H in order. For each:
   client sends ``"<sock_name> HANDLE=<int>\\0"``, server replies
   ``"READY\\0"``.
5. After all five, server sends a final ``"READY\\0"`` on CTRL.

All 5 client sockets target the same TCP port. The listener tracks a
3-state machine — ``idle`` / ``accepting`` / ``session`` — to route
each incoming connection appropriately.
"""

import asyncio
import logging
import secrets

from ..protocol import jtag

from . import control
from .session import JopSession


_logger = logging.getLogger("jop.listener")


_StreamPair = tuple[asyncio.StreamReader, asyncio.StreamWriter]


class JopListener:
    """Bind, accept, hand off to :class:`JopSession`."""

    STATE_IDLE = "idle"
    STATE_ACCEPTING = "accepting"
    STATE_SESSION = "session"

    def __init__(self, interface: jtag.JtagInterface, *,
                 host: str = "0.0.0.0", port: int = 1259,
                 mgmt_support: bool = False,
                 logger: logging.Logger | None = None) -> None:
        self._interface = interface
        self._host = host
        self._port = port
        self._mgmt_support = mgmt_support
        self.logger = logger or _logger
        self._server: asyncio.base_events.Server | None = None
        self._state = self.STATE_IDLE
        # Connections accepted between CTRL handshake and the start of
        # the data session. The handshake driver pulls from this queue.
        self._handshake_queue: asyncio.Queue[_StreamPair] = asyncio.Queue()

    async def serve_forever(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connect, self._host, self._port)
        addrs = ", ".join(str(s.getsockname())
                          for s in self._server.sockets)
        self.logger.info("JoP listener on %s for %s",
                         addrs, self._interface.fqdn)
        async with self._server:
            await self._server.serve_forever()

    @property
    def server_port(self) -> int:
        if self._server is None:
            raise RuntimeError("listener not started")
        return self._server.sockets[0].getsockname()[1]

    @property
    def busy(self) -> bool:
        return self._state != self.STATE_IDLE

    async def _on_connect(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if self._state == self.STATE_IDLE:
            self.logger.info("CTRL connection from %s", peer)
            self._state = self.STATE_ACCEPTING
            try:
                await self._run_session((reader, writer))
            finally:
                self._state = self.STATE_IDLE
                # Drain any queued connections that didn't make it
                # into the handshake (rare — caused by a misbehaving
                # client opening too many sockets).
                while not self._handshake_queue.empty():
                    _, w = self._handshake_queue.get_nowait()
                    w.close()
            return

        if self._state == self.STATE_ACCEPTING:
            # Hand off to the handshake driver and hold the connection
            # open until the session decides to close it.
            await self._handshake_queue.put((reader, writer))
            await self._wait_until_closed(writer)
            return

        # STATE_SESSION → reject.
        self.logger.info("Rejecting %s — session active", peer)
        writer.write(control.REJECT_MSG)
        try:
            await writer.drain()
        except Exception:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    @staticmethod
    async def _wait_until_closed(writer: asyncio.StreamWriter) -> None:
        # asyncio.StreamWriter doesn't fire an event when *we* close it
        # locally; poll cheaply. The session closes all 5 writers from
        # one place so all 5 _on_connect calls return roughly together.
        while not writer.is_closing():
            await asyncio.sleep(0.05)
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _run_session(self, ctrl_pair: _StreamPair) -> None:
        ctrl_reader, ctrl_writer = ctrl_pair
        try:
            sockets = await self._handshake(ctrl_reader, ctrl_writer)
        except Exception as e:
            self.logger.warning("Handshake failed: %s", e)
            ctrl_writer.close()
            return

        self._state = self.STATE_SESSION
        session = JopSession(
            interface=self._interface,
            ctrl=(ctrl_reader, ctrl_writer),
            mgmt=sockets["mgmt"],
            mgmt_rsp=sockets["mgmt_rsp"],
            h2t=sockets["h2t"],
            t2h=sockets["t2h"],
            mgmt_support=self._mgmt_support,
        )
        try:
            await session.serve()
        finally:
            for pair in (ctrl_pair, sockets["mgmt"], sockets["mgmt_rsp"],
                         sockets["h2t"], sockets["t2h"]):
                _, w = pair
                if not w.is_closing():
                    w.close()

    async def _handshake(self, ctrl_reader, ctrl_writer
                          ) -> dict[str, _StreamPair]:
        handle = self._random_handle()
        welcome = control.welcome_message(
            mgmt_support=1 if self._mgmt_support else 0,
            h2t_rx_buff_sz=control.DEFAULT_H2T_BUFF_SZ,
            mgmt_rx_buff_sz=control.DEFAULT_MGMT_BUFF_SZ,
            ctrl_rx_buff_sz=control.DEFAULT_CTRL_BUFF_SZ,
            handle=handle,
        )
        ctrl_writer.write(welcome)
        await ctrl_writer.drain()

        # CTRL ack
        await self._expect_handle(
            ctrl_reader, control.SOCK_CONTROL, handle)
        ctrl_writer.write(control.READY_MSG)
        await ctrl_writer.drain()

        names = [
            ("mgmt", control.SOCK_MGMT),
            ("mgmt_rsp", control.SOCK_MGMT_RSP),
            ("h2t", control.SOCK_H2T),
            ("t2h", control.SOCK_T2H),
        ]
        result: dict[str, _StreamPair] = {}
        for slot, sock_name in names:
            pair = await self._handshake_queue.get()
            r, w = pair
            try:
                await self._expect_handle(r, sock_name, handle)
            except Exception:
                w.write(control.NOT_READY_MSG)
                await w.drain()
                w.close()
                raise
            w.write(control.READY_MSG)
            await w.drain()
            result[slot] = pair

        # Final READY on CTRL signals "all five sockets bound, go".
        ctrl_writer.write(control.READY_MSG)
        await ctrl_writer.drain()
        return result

    @staticmethod
    async def _expect_handle(reader: asyncio.StreamReader,
                              sock_name: str, handle: int) -> None:
        line = await reader.readuntil(b"\0")
        expected = control.expected_handle_message(sock_name, handle)
        if line != expected:
            raise ValueError(
                f"bad handle ack on {sock_name}: got {line!r}, "
                f"expected {expected!r}")

    @staticmethod
    def _random_handle() -> int:
        # Non-zero per Intel's reference.
        v = 0
        while v == 0:
            v = secrets.randbits(31)
        return v
