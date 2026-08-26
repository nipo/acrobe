"""Memory — batched access to a flat address space.

Any node that lets the host read and write bytes at addresses speaks
this protocol: an ARM MEM-AP bridging into a chip's AHB, the DP's own
system address space, a SPI NOR flash, a ROM bootloader's USB command
set. They differ wildly in what a single wire transaction costs and in
what granularity the hardware natively supports, so the surface is
split into two op families and each implementer takes only the ones
its bus can honour.

* **Register family** — ``read8/16/32(addr)`` and
  ``write8/16/32(addr, value)``. Naturally-sized, naturally-aligned
  accesses; results are ints. This is what register-mapped debug
  fabric wants.
* **Bulk family** — ``mem_read(addr, size)`` and
  ``mem_write(addr, data)``. Byte-granular blocks; results are
  ``bytes``. This is what memory images want.

À la carte means à la carte: :class:`Interface` rejects an op the
implementer never declared (:class:`UnsupportedAccess`) instead of
faking it. A SPI flash has no register window; a register-only debug
fabric has no meaningful byte-granular block access.

When a bus natively serves one family and the other is still wanted,
one of the emulation mixins bridges the gap:

* :class:`BulkFromRegister` splits blob ops into register-width
  sub-ops, then hands the flat register-op stream to
  ``lower_register_ops``. That seam is the whole point: a backend
  with its own wire encoding overrides only the wire step and
  inherits blob support unchanged.
* :class:`RegisterFromBulk` goes the other way, serving register
  accessors from 1/2/4-byte blob ops.
* :class:`BackgroundLowering` adapts a plain-coroutine backend (USB
  command set, SPI command sequence) to the no-IO-in-``flush_ops``
  contract.

Read decomposition has two strategies, selected per implementer via
the ``decomposition`` class attribute:
:class:`PreciseDecomposition` touches exactly the requested bytes;
:class:`OverwideDecomposition` covers the range with aligned 32-bit
reads and trims the edges on reassembly, trading a few extra in-word
byte reads for fewer ops and a constant access size. Writes are
precise under both — extra bytes cannot be un-written.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from ..engine import chain_future


# --- Op dataclasses (frozen, inputs only) --------------------------

@dataclass(frozen=True, slots=True)
class Read8:
    addr: int

    def __repr__(self):
        return f"Read8({self.addr:#x})"


@dataclass(frozen=True, slots=True)
class Read16:
    addr: int

    def __repr__(self):
        return f"Read16({self.addr:#x})"


@dataclass(frozen=True, slots=True)
class Read32:
    addr: int

    def __repr__(self):
        return f"Read32({self.addr:#x})"


@dataclass(frozen=True, slots=True)
class Write8:
    addr: int
    data: int

    def __repr__(self):
        return f"Write8({self.addr:#x}, {self.data:#04x})"


@dataclass(frozen=True, slots=True)
class Write16:
    addr: int
    data: int

    def __repr__(self):
        return f"Write16({self.addr:#x}, {self.data:#06x})"


@dataclass(frozen=True, slots=True)
class Write32:
    addr: int
    data: int

    def __repr__(self):
        return f"Write32({self.addr:#x}, {self.data:#010x})"


@dataclass(frozen=True, slots=True)
class ReadBlob:
    """Byte-granular read of ``size`` bytes from ``addr``. The future
    resolves with exactly ``size`` bytes."""

    addr: int
    size: int

    def __repr__(self):
        return f"ReadBlob({self.addr:#x}, {self.size})"


@dataclass(frozen=True, slots=True)
class WriteBlob:
    """Byte-granular write of ``data`` at ``addr``. The future
    resolves with ``None`` once the bytes have committed."""

    addr: int
    data: bytes

    def __repr__(self):
        return f"WriteBlob({self.addr:#x}, {len(self.data)} B)"


class UnsupportedAccess(Exception):
    """The address space does not implement the requested op family."""


# --- Blob completion aggregator -----------------------------------

class PendingBlob:
    """Resolves one blob op's future once every register-width sub-op
    it was split into has completed.

    Sub-op futures are hooked through
    :py:meth:`asyncio.Future.add_done_callback`, so nothing here ever
    awaits — it counts completions and runs a final assembly step.

    Read sub-ops may reach outside the requested range (the overwide
    strategy deliberately does), so assembly clips each sub-result to
    the blob's own window.

    A read sub-future carries either an int (register-width sub-op,
    little-endian on the wire) or the raw bytes of the covered span —
    a backend whose native access already returns a byte block splits
    a blob into byte-block sub-ops and reassembles them here.
    """

    def __init__(self, user_future: asyncio.Future, size: int,
                 is_read: bool):
        self.user_future = user_future
        self.size = size
        self.is_read = is_read
        self.sub_futures: list[tuple[int, int, asyncio.Future]] = []
        self.remaining = 0
        self.exception: BaseException | None = None

    def attach(self, offset: int, size_bytes: int,
               sub_future: asyncio.Future) -> None:
        self.sub_futures.append((offset, size_bytes, sub_future))
        self.remaining += 1
        sub_future.add_done_callback(self.__on_done)

    def __on_done(self, sub_future: asyncio.Future) -> None:
        self.remaining -= 1
        if self.exception is None:
            exc = sub_future.exception()
            if exc is not None:
                self.exception = exc
        if self.remaining == 0:
            self.__resolve()

    def __resolve(self) -> None:
        if self.user_future.done():
            return
        if self.exception is not None:
            self.user_future.set_exception(self.exception)
            return
        if not self.is_read:
            self.user_future.set_result(None)
            return
        buf = bytearray(self.size)
        for offset, size_bytes, future in self.sub_futures:
            lo = max(0, offset)
            hi = min(self.size, offset + size_bytes)
            if hi <= lo:
                continue
            raw = future.result()
            if not isinstance(raw, (bytes, bytearray)):
                raw = raw.to_bytes(size_bytes, "little")
            buf[lo:hi] = raw[lo - offset:hi - offset]
        self.user_future.set_result(bytes(buf))


# --- Blob decomposition strategies --------------------------------

class Decomposition:
    """Splits a byte range into register-width ops.

    Both entry points return ``(op, blob_offset, size_bytes)``
    triples. ``blob_offset`` may be negative and ``blob_offset +
    size_bytes`` may exceed the blob's length for read strategies
    that deliberately over-read; :class:`PendingBlob` clips.
    """

    READ_OP = {1: Read8, 2: Read16, 4: Read32}
    WRITE_OP = {1: Write8, 2: Write16, 4: Write32}
    UNPACK = {1: "<B", 2: "<H", 4: "<I"}

    @staticmethod
    def chunks(addr: int, size: int) -> list[tuple[int, int, int]]:
        """Cover ``[addr, addr + size)`` with naturally-aligned 1/2/4
        byte chunks: peel a leading byte, peel a leading halfword,
        stream the aligned middle as words, peel a trailing halfword,
        peel a trailing byte.

        Returns ``(blob_offset, chunk_addr, chunk_size)`` triples."""
        out: list[tuple[int, int, int]] = []
        cursor = addr
        end = addr + size
        offset = 0

        def take(n: int) -> None:
            nonlocal cursor, offset
            out.append((offset, cursor, n))
            cursor += n
            offset += n

        if (cursor & 1) and end > cursor:
            take(1)
        if (cursor & 3) == 2 and end - cursor >= 2:
            take(2)
        while end - cursor >= 4:
            take(4)
        if end - cursor >= 2:
            take(2)
        if end - cursor >= 1:
            take(1)
        return out

    @classmethod
    def read_ops(cls, addr: int, size: int):
        raise NotImplementedError

    @classmethod
    def write_ops(cls, addr: int, data: bytes):
        """Writes are always precise: a bus cannot be handed bytes it
        must not commit, so no strategy may widen a write."""
        return [
            (cls.WRITE_OP[n](
                chunk_addr,
                struct.unpack_from(cls.UNPACK[n], data, offset)[0]),
             offset, n)
            for offset, chunk_addr, n in cls.chunks(addr, len(data))
        ]


class PreciseDecomposition(Decomposition):
    """Touch exactly the requested bytes, nothing else. The only safe
    choice on a bus where a read has side effects or where addresses
    outside the range may fault."""

    @classmethod
    def read_ops(cls, addr: int, size: int):
        return [(cls.READ_OP[n](chunk_addr), offset, n)
                for offset, chunk_addr, n in cls.chunks(addr, size)]


class OverwideDecomposition(Decomposition):
    """Cover the range with aligned 32-bit reads and trim the edges.

    Costs up to three extra byte reads at each end — always inside a
    word the range already touches — and buys a uniform access size,
    fewer ops, and an address stream a sequential-access engine can
    ride end to end."""

    @classmethod
    def read_ops(cls, addr: int, size: int):
        if size <= 0:
            return []
        first = addr & ~3
        last = (addr + size + 3) & ~3
        return [(Read32(word_addr), word_addr - addr, 4)
                for word_addr in range(first, last, 4)]


# --- Abstract surface ---------------------------------------------

class Interface:
    """Batched access to a flat address space.

    Mixed into a :class:`~acrobe.engine.Batcher`: every accessor posts
    a frozen op and returns the future the batcher will resolve.

    ``ops`` declares which op classes this surface's lowering accepts.
    Anything else raises :class:`UnsupportedAccess` at the call site.
    Surfaces that reach their backend without batching (a pure
    forwarder) override the individual accessors instead of relying on
    ``submit``.
    """

    REGISTER_OPS = frozenset({Read8, Read16, Read32,
                              Write8, Write16, Write32})
    BULK_OPS = frozenset({ReadBlob, WriteBlob})

    ops: frozenset = frozenset()

    def submit(self, op):
        if type(op) not in self.ops:
            raise UnsupportedAccess(
                f"{type(self).__name__} does not implement "
                f"{type(op).__name__}")
        return self.post(op)

    def read8(self, addr: int):
        return self.submit(Read8(addr))

    def read16(self, addr: int):
        return self.submit(Read16(addr))

    def read32(self, addr: int):
        return self.submit(Read32(addr))

    def write8(self, addr: int, data: int):
        return self.submit(Write8(addr, data))

    def write16(self, addr: int, data: int):
        return self.submit(Write16(addr, data))

    def write32(self, addr: int, data: int):
        return self.submit(Write32(addr, data))

    def mem_read(self, addr: int, size: int):
        return self.submit(ReadBlob(addr, size))

    def mem_write(self, addr: int, data):
        return self.submit(WriteBlob(addr, bytes(data)))


# --- Emulation mixins ---------------------------------------------

class BulkFromRegister(Interface):
    """Serve the bulk family from the register family.

    ``flush_ops`` splits every blob op in the batch into
    register-width sub-ops and hands the resulting flat register-op
    stream — in batch order, blob sub-ops inlined where their blob sat
    — to :meth:`lower_register_ops`.

    Splitting and wire encoding are separate steps on purpose: a
    backend whose firmware speaks its own memory commands overrides
    ``lower_register_ops`` alone and keeps working blob support.
    """

    decomposition: type[Decomposition] = PreciseDecomposition

    async def flush_ops(self, batch):
        loop = asyncio.get_running_loop()
        lowered: list[tuple[object, asyncio.Future | None]] = []
        for op, future in batch:
            if isinstance(op, ReadBlob):
                lowered.extend(self.__expand_read(op, future, loop))
            elif isinstance(op, WriteBlob):
                lowered.extend(self.__expand_write(op, future, loop))
            else:
                lowered.append((op, future))
        self.lower_register_ops(lowered)

    def lower_register_ops(self, batch):
        """Translate a batch of register-width ops onto the wire and
        resolve every non-None future. Must not await IO."""
        raise NotImplementedError

    def __expand_read(self, op, future, loop):
        triples = self.decomposition.read_ops(op.addr, op.size)
        if future is None:
            return [(sub, None) for sub, _, _ in triples]
        if not triples:
            future.set_result(b"")
            return []
        pending = PendingBlob(future, op.size, is_read=True)
        return [(sub, self.__sub_future(pending, offset, n, loop))
                for sub, offset, n in triples]

    def __expand_write(self, op, future, loop):
        triples = self.decomposition.write_ops(op.addr, op.data)
        if future is None:
            return [(sub, None) for sub, _, _ in triples]
        if not triples:
            future.set_result(None)
            return []
        pending = PendingBlob(future, len(op.data), is_read=False)
        return [(sub, self.__sub_future(pending, offset, n, loop))
                for sub, offset, n in triples]

    @staticmethod
    def __sub_future(pending, offset, size_bytes, loop):
        sub_future = loop.create_future()
        pending.attach(offset, size_bytes, sub_future)
        return sub_future


class RegisterFromBulk(Interface):
    """Serve the register family from the bulk family.

    For a bus whose native access is a byte block: each register
    accessor posts a 1/2/4-byte blob op and adapts the payload.

    ``register_endianness`` is the byte order the device lays a
    multi-byte register out in. Memory-mapped fabric is little-endian;
    I2C and SPI device register maps are conventionally big-endian.
    """

    register_endianness: str = "little"

    def read8(self, addr: int):
        return self.__read(addr, 1)

    def read16(self, addr: int):
        return self.__read(addr, 2)

    def read32(self, addr: int):
        return self.__read(addr, 4)

    def write8(self, addr: int, data: int):
        return self.__write(addr, data, 1)

    def write16(self, addr: int, data: int):
        return self.__write(addr, data, 2)

    def write32(self, addr: int, data: int):
        return self.__write(addr, data, 4)

    def __read(self, addr: int, size_bytes: int):
        result = asyncio.get_running_loop().create_future()
        order = self.register_endianness
        chain_future(self.submit(ReadBlob(addr, size_bytes)), result,
                     lambda raw: int.from_bytes(raw, order))
        return result

    def __write(self, addr: int, value: int, size_bytes: int):
        payload = (value & ((1 << (size_bytes * 8)) - 1)).to_bytes(
            size_bytes, self.register_endianness)
        return self.submit(WriteBlob(addr, payload))


class BackgroundLowering:
    """Run a batch against a plain-coroutine backend.

    Some address spaces sit on a command-oriented backend — a USB
    vendor command set, an SPI command sequence with busy polling —
    rather than on another Batcher, so their lowering has to await.
    ``dispatch(batch)`` hands the batch to a background task and
    returns immediately, keeping ``flush_ops`` free of IO; a lock
    holds batches in submission order.

    Subclasses implement :meth:`run_ops`, which may await freely and
    must resolve every non-None future.
    """

    __lock: asyncio.Lock | None = None

    def dispatch(self, batch) -> None:
        if self.__lock is None:
            self.__lock = asyncio.Lock()
        asyncio.ensure_future(self.__drain(batch, self.__lock))

    async def __drain(self, batch, lock: asyncio.Lock) -> None:
        async with lock:
            try:
                await self.run_ops(batch)
            except Exception as exc:
                self.__reject(batch, exc)
            else:
                self.__reject(batch, None)

    def __reject(self, batch, exc: BaseException | None) -> None:
        for op, future in batch:
            if future is None or future.done():
                continue
            future.set_exception(exc if exc is not None else RuntimeError(
                f"{type(self).__name__} left {op!r} unresolved"))

    async def run_ops(self, batch):
        raise NotImplementedError
