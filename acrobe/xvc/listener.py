"""TCP listener for XVC sessions.

Single-client policy: while one session is active, additional
connections are closed immediately. XVC has no in-band "busy" reply,
so dropping the socket is the only signal we can send. Vivado
handles the close cleanly and surfaces it as "cable unavailable".
"""

import asyncio
import logging

from ..protocol import jtag

from .session import XvcSession


_logger = logging.getLogger("xvc.listener")


class XvcListener:
    """Bind, accept, hand off to :class:`XvcSession` one at a time."""

    DEFAULT_PORT = 2542  # Xilinx convention; matches Vivado defaults.

    def __init__(self, interface: jtag.JtagInterface, *,
                 host: str = "0.0.0.0",
                 port: int = DEFAULT_PORT,
                 logger: logging.Logger | None = None) -> None:
        self._interface = interface
        self._host = host
        self._port = port
        self.logger = logger or _logger
        self._server: asyncio.base_events.Server | None = None
        # Held while a session is running. New connections that arrive
        # while held are closed.
        self._busy = False

    @property
    def server_port(self) -> int:
        if self._server is None:
            raise RuntimeError("listener not started")
        return self._server.sockets[0].getsockname()[1]

    @property
    def busy(self) -> bool:
        return self._busy

    async def serve_forever(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connect, self._host, self._port)
        addrs = ", ".join(str(s.getsockname())
                          for s in self._server.sockets)
        self.logger.info("XVC listener on %s for %s",
                         addrs, self._interface.fqdn)
        async with self._server:
            await self._server.serve_forever()

    async def _on_connect(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if self._busy:
            self.logger.info(
                "Rejecting %s — XVC session already active", peer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return

        self._busy = True
        self.logger.info("XVC client %s connected", peer)
        session = XvcSession(reader, writer, self._interface)
        try:
            await session.serve()
        finally:
            self._busy = False
            self.logger.info("XVC client %s disconnected", peer)
            if not writer.is_closing():
                writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
