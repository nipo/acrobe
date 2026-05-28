"""Generic UDP adapter — exposes a remote socket as a :class:`Datagram`.

Path syntax::

    udp/<host>:<port>/...

`udp` is matched by :class:`UdpEnumerator` and resolves to a
:class:`UdpBroker`. The broker parses the next path component as
``host:port`` and returns a :class:`UdpDatagram` connected to that
peer.

The socket is opened lazily in :meth:`UdpDatagram.start` (via
``loop.create_datagram_endpoint(..., remote_addr=...)``) and torn
down in :meth:`UdpDatagram.stop`. Incoming packets are buffered in
an :class:`asyncio.Queue` and dispatched to pending
:class:`acrobe.protocol.datagram.Recv` ops in FIFO order.

Sub-handlers registered against :attr:`Datagram.db` are reachable
as children of the datagram node (``udp/host:port/<handler>``).
"""

from __future__ import annotations

import asyncio
import re

from ..db import NoMatch
from ..lifecycle import cancel_shutdown, on_shutdown
from ..node import Node
from ..protocol.datagram import Datagram, Recv, Send
from .model import Enumerator, enumerator_db


_HOST_PORT_RE = re.compile(
    r"^(?P<host>"
    r"\[[0-9A-Fa-f:]+\]"
    r"|[A-Za-z0-9._\-]+)"
    r":(?P<port>\d+)$")


def _parse_host_port(name: str) -> tuple[str, int]:
    m = _HOST_PORT_RE.match(name)
    if not m:
        raise ValueError(f"not a host:port: {name!r}")
    host = m.group("host")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, int(m.group("port"))


class _UdpProtocol(asyncio.DatagramProtocol):
    """Inbound-packet sink — pushes every datagram onto the owner's
    receive queue. Outbound traffic goes through the transport
    directly."""

    def __init__(self, queue: asyncio.Queue):
        self.__queue = queue

    def datagram_received(self, data: bytes, addr) -> None:
        self.__queue.put_nowait((data, addr))

    def error_received(self, exc) -> None:
        # ICMP "port unreachable" and friends. Surface to the queue so
        # the next Recv resolves with the exception rather than hanging.
        self.__queue.put_nowait(exc)

    def connection_lost(self, exc) -> None:
        # Endpoint closed (clean or with error). A sentinel lets a
        # pending Recv break out instead of waiting forever.
        self.__queue.put_nowait(exc or ConnectionError("UDP endpoint closed"))


class UdpDatagram(Datagram):
    """:class:`Datagram` over a connected :mod:`asyncio` UDP endpoint.

    Each :class:`Send` is delivered to the peer set at construction;
    each :class:`Recv` resolves with the next buffered packet (FIFO)
    as ``(data, src_addr)``.
    """

    def __init__(self, host: str, port: int, name: str | None = None):
        super().__init__(name or f"{host}:{port}")
        self.__host = host
        self.__port = port
        self.__transport: asyncio.DatagramTransport | None = None
        self.__rx_queue: asyncio.Queue = asyncio.Queue()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.__transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self.__rx_queue),
            remote_addr=(self.__host, self.__port))
        self.logger.info("bound to udp://%s:%d", self.__host, self.__port)
        on_shutdown(self.stop)

    async def stop(self) -> None:
        cancel_shutdown(self.stop)
        transport = self.__transport
        self.__transport = None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                try:
                    if self.__transport is None:
                        raise ConnectionError("UdpDatagram not started")
                    self.__transport.sendto(op.data)
                except Exception as exc:
                    if future is not None and not future.done():
                        future.set_exception(exc)
                    continue
                if future is not None and not future.done():
                    future.set_result(None)
            elif isinstance(op, Recv):
                asyncio.create_task(self.__recv_task(future))
            else:
                if future is not None and not future.done():
                    future.set_exception(TypeError(
                        f"{type(self).__name__}: unsupported op "
                        f"{type(op).__name__}"))

    async def __recv_task(self, future):
        try:
            item = await self.__rx_queue.get()
        except Exception as exc:
            if future is not None and not future.done():
                future.set_exception(exc)
            return
        if isinstance(item, BaseException):
            if future is not None and not future.done():
                future.set_exception(item)
            return
        data, addr = item
        if future is not None and not future.done():
            future.set_result((data, addr))


class UdpBroker(Node):
    """The ``udp`` directory under :class:`HwRoot`.

    Resolves ``udp/<host>:<port>`` to a fresh :class:`UdpDatagram`.
    """

    def __init__(self, name: str = "udp"):
        super().__init__(name)

    async def child_spawn(self, name: str) -> UdpDatagram:
        try:
            host, port = _parse_host_port(name)
        except ValueError:
            raise NoMatch("udp-endpoint", name)
        return UdpDatagram(host=host, port=port, name=name)


class UdpEnumerator(Enumerator):
    """Attaches the single :class:`UdpBroker` namespace node."""

    async def populate(self, hw_root):
        if not hw_root.has_child("udp"):
            hw_root.child_add(UdpBroker())


enumerator_db.register("udp")(UdpEnumerator)
