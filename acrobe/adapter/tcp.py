"""Generic TCP adapter — exposes a remote socket as a :class:`Pipe`.

Path syntax::

    tcp/<host>[:<port>]/...

`tcp` is matched by :class:`TcpEnumerator` and resolves to a
:class:`TcpBroker`. The broker parses the next path component as
``host:port`` (port mandatory — no implicit default for a generic
adapter) and returns a :class:`TcpPipe`, which IS the pipe node.

The pipe lazily opens the TCP connection in :meth:`TcpPipe.start`
and tears it down in :meth:`TcpPipe.stop`; it also registers itself
with the process-wide :mod:`acrobe.lifecycle` so an abrupt exit
still drains the socket.

Sub-handlers registered against :attr:`Pipe.db` are reachable as
children of the pipe (e.g. ``tcp/host:port/<protocol-handler>``),
mirroring crobe's bridging pattern.
"""

from __future__ import annotations

import asyncio
import re

from ..db import NoMatch
from ..lifecycle import cancel_shutdown, on_shutdown
from ..node import Node
from ..protocol.pipe import Pipe, Read, Write
from .model import Enumerator, enumerator_db


_HOST_PORT_RE = re.compile(
    r"^(?P<host>"
    r"\[[0-9A-Fa-f:]+\]"               # IPv6 in brackets
    r"|[A-Za-z0-9._\-]+)"              # hostname or IPv4
    r":(?P<port>\d+)$")


def _parse_host_port(name: str) -> tuple[str, int]:
    m = _HOST_PORT_RE.match(name)
    if not m:
        raise ValueError(f"not a host:port: {name!r}")
    host = m.group("host")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, int(m.group("port"))


class TcpPipe(Pipe):
    """:class:`Pipe` over an :mod:`asyncio` TCP stream.

    Concurrent writes are serialised against each other; concurrent
    reads are dispatched as background tasks so a long-blocking read
    doesn't stall pending writes (or other reads).
    """

    def __init__(self, host: str, port: int, name: str | None = None):
        super().__init__(name or f"{host}:{port}")
        self.__host = host
        self.__port = port
        self.__reader: asyncio.StreamReader | None = None
        self.__writer: asyncio.StreamWriter | None = None
        self.__write_lock = asyncio.Lock()

    async def start(self) -> None:
        self.__reader, self.__writer = await asyncio.open_connection(
            self.__host, self.__port)
        self.logger.info("connected to tcp://%s:%d", self.__host, self.__port)
        on_shutdown(self.stop)

    async def stop(self) -> None:
        cancel_shutdown(self.stop)
        writer = self.__writer
        self.__writer = None
        self.__reader = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Write):
                asyncio.create_task(self.__write_task(op.data, future))
            elif isinstance(op, Read):
                asyncio.create_task(self.__read_task(op.size, future))
            else:
                if future is not None and not future.done():
                    future.set_exception(TypeError(
                        f"{type(self).__name__}: unsupported op "
                        f"{type(op).__name__}"))

    async def __write_task(self, data, future):
        try:
            async with self.__write_lock:
                if self.__writer is None:
                    raise ConnectionError("TcpPipe not started")
                self.__writer.write(data)
                await self.__writer.drain()
        except Exception as exc:
            if future is not None and not future.done():
                future.set_exception(exc)
            return
        if future is not None and not future.done():
            future.set_result(None)

    # Upper bound on a streaming (size=None) read. Picked to comfortably
    # exceed a TCP MTU so a single recv() consumes whatever the kernel
    # delivered in one go.
    __STREAM_CHUNK = 65536

    async def __read_task(self, size, future):
        try:
            if self.__reader is None:
                raise ConnectionError("TcpPipe not started")
            if size is None:
                data = await self.__reader.read(self.__STREAM_CHUNK)
                if not data:
                    raise EOFError("TcpPipe: connection closed")
            else:
                data = await self.__reader.readexactly(size)
        except Exception as exc:
            if future is not None and not future.done():
                future.set_exception(exc)
            return
        if future is not None and not future.done():
            future.set_result(data)


class TcpBroker(Node):
    """The ``tcp`` directory under :class:`HwRoot`.

    Resolves ``tcp/<host>:<port>`` to a fresh :class:`TcpPipe`. Owns
    no state of its own.
    """

    def __init__(self, name: str = "tcp"):
        super().__init__(name)

    async def child_spawn(self, name: str) -> TcpPipe:
        try:
            host, port = _parse_host_port(name)
        except ValueError:
            raise NoMatch("tcp-endpoint", name)
        return TcpPipe(host=host, port=port, name=name)


@enumerator_db.register("tcp")
class TcpEnumerator(Enumerator):
    """Attaches the single :class:`TcpBroker` namespace node."""

    async def populate(self, hw_root):
        if not hw_root.has_child("tcp"):
            hw_root.child_add(TcpBroker())
