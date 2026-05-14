"""Pipe — abstract byte-stream transport.

A `Pipe` is a full-duplex, byte-accurate transport. Reads return
exactly the bytes requested; writes deliver every byte. There is no
framing, no addressing, no message boundary on the wire.

Like the rest of acrobe's protocol layers, `Pipe` is a
``Batcher + Node``: callers post `Read` / `Write` op dataclasses via
`post(op)` (or the `read` / `write` shortcuts) and receive futures
that resolve when the op completes. Concrete subclasses implement
`flush_ops` to drive the underlying transport (USB bulk pair, TCP
socket, telnet stream, …).

Call sites use the futures transparently::

    data = await pipe.read(n)
    await pipe.write(payload)

— so existing code keeps the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine import Batcher
from ..node import Node


@dataclass(frozen=True, slots=True)
class Write:
    """Write `data` to the pipe. Future resolves to ``None``."""

    data: bytes


@dataclass(frozen=True, slots=True)
class Read:
    """Read exactly `size` bytes. Future resolves to a ``bytes`` of
    length `size`."""

    size: int


class Pipe(Batcher, Node):
    """Abstract byte-stream transport.

    Concrete subclasses (USB bulk, TCP, telnet, in-memory loopback)
    implement :meth:`flush_ops` to translate batched `Read` /
    `Write` ops into transport-level operations.
    """

    def __init__(self, name: str = "pipe"):
        Batcher.__init__(self)
        Node.__init__(self, name)

    def write(self, data: bytes):
        """Post a Write op. Returns a Future resolving to ``None``."""
        return self.post(Write(bytes(data)))

    def read(self, size: int):
        """Post a Read op. Returns a Future resolving to ``bytes``."""
        return self.post(Read(int(size)))

    async def flush_ops(self, batch):
        raise NotImplementedError(
            f"{type(self).__name__} must implement flush_ops")
