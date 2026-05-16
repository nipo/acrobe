"""Datagram — abstract framed-message transport with routing context.

A `Datagram` carries discrete messages with end-of-frame markers
(unlike `Pipe`, which is an unframed byte stream). Each message
optionally carries a `context` value identifying source / destination
endpoints on multi-endpoint transports (NSL bnoc routed FIFOs, AJI
sessions, …).

Like the other acrobe protocol layers, `Datagram` is a
``Batcher + Node``: callers post `Send` / `Recv` op dataclasses via
`post(op)` (or the `send` / `recv` shortcuts) and receive futures
that resolve when the op completes. Concrete subclasses implement
`flush_ops`.

Call-site usage::

    await dg.send(data, context=route_to_peer)
    payload, src_ctx = await dg.recv(context=route_from_peer)

For plain framed channels without routing the `context` is `None` on
both directions and the recv tuple's second element is `None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..db import Db
from ..engine import Batcher
from ..node import Node


@dataclass(frozen=True, slots=True)
class Send:
    """Send `data` as one message.

    `context` is opaque to `Datagram` — concrete implementations
    interpret it as routing / addressing metadata.

    The future returned by ``Batcher.post`` resolves to ``None``."""

    data: bytes
    context: Any = None


@dataclass(frozen=True, slots=True)
class Recv:
    """Receive one message.

    `context` is an opaque filter / addressing hint. Concrete
    implementations that route may use it to select which message to
    consume; non-routing implementations ignore it.

    The future returned by ``Batcher.post`` resolves to a tuple
    ``(data: bytes, recv_context)``. `recv_context` is the context
    associated with the received message, or ``None`` for
    implementations without routing."""

    context: Any = None


class Datagram(Batcher, Node):
    """Abstract framed-message transport.

    Concrete subclasses (NSL bnoc framed FIFO, routed wrappers, USB
    bulk pair with short-packet framing, …) implement
    :meth:`flush_ops` to translate batched `Send` / `Recv` ops into
    transport-level operations.

    Like :class:`acrobe.protocol.pipe.Pipe`, a `Datagram` is a bridge
    point: handlers registered against :attr:`db` are spawned via
    ``child_summon(name)`` with the datagram as their parent
    transport (routed wrappers, protocol decoders, …).
    """

    db = Db("Datagram handler")

    def __init__(self, name: str = "datagram"):
        Batcher.__init__(self)
        Node.__init__(self, name)

    def send(self, data: bytes, context: Any = None):
        """Post a Send op. Returns a Future resolving to ``None``."""
        return self.post(Send(bytes(data), context))

    def recv(self, context: Any = None):
        """Post a Recv op. Returns a Future resolving to
        ``(data, recv_context)``."""
        return self.post(Recv(context))

    async def flush_ops(self, batch):
        raise NotImplementedError(
            f"{type(self).__name__} must implement flush_ops")

    async def child_spawn(self, name):
        return await self.db.acall(name, self)
