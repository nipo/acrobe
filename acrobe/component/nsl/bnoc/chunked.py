"""NSL bnoc chunked framing over a Pipe.

Matches RTL ``nsl_bnoc.framed.framed_chunker`` /
``framed_unchunker``: a framed (LAST-marked AXI-stream) channel is
carried over an unframed byte pipe by cutting each logical frame into
length-prefixed chunks. Unlike ``nsl_bnoc.sized`` (a single 16-bit
length prefix, so a hard 64 KiB cap and a whole-frame buffer on the
device side), a logical frame here is an arbitrary number of chunks
reassembled at the ``LAST`` marker — so frames are unbounded and the
device-side framer only ever buffers one chunk (its FIFO depth).

Wire encoding, byte 0 selects control vs. data by its MSB:

* ``0b11xxxxxx`` — RESET: hold the whole downstream in reset. Single
  byte; the low 6 bits are don't-care, so a spray of any such byte is
  a valid marker.
* ``0b10xxxxxx`` — RELEASE: release the downstream reset. Single byte;
  low 6 bits don't-care.
* ``0b0Lnnnnnn 0bnnnnnnnn`` — data chunk header. ``L`` is the LAST
  flag (final chunk of the datagram); the 14-bit ``n`` field is
  ``size - 1`` (payload 1..16384 bytes), followed by the payload.

Because the only byte run a control marker cannot interrupt is a
chunk payload — and payloads are length-bounded — spraying more than
one max chunk worth of RESET bytes is guaranteed to overflow any
chunk a half-written frame left the device parsing, after which the
markers land on a header boundary and take effect. The host does this
on :meth:`start` (the FT245/USB boundary carries no framing of its
own, so a host that died mid-write can leave a dangling frame in the
device FIFO engine).
"""

from __future__ import annotations

import asyncio
from collections import deque

from ....engine import chain_future
from ....protocol.datagram import Datagram, Send, Recv
from ....protocol.pipe import Pipe

@Pipe.db.register("chunked")
class Chunked(Datagram):
    """Datagram over a `Pipe` backend with length-prefixed chunking."""

    # Byte-0 MSB: 1 = single-byte control marker, 0 = data chunk header.
    CTRL_FLAG = 0x80

    # Control markers (single byte; low 6 bits don't-care).
    RESET   = 0xc0   # 0b11xxxxxx: assert downstream reset
    RELEASE = 0x80   # 0b10xxxxxx: release downstream reset

    # Data chunk header byte 0.
    LAST_FLAG    = 0x40   # bit 6: final chunk of the datagram
    SIZE_HI_MASK = 0x3f   # bits 5:0: high 6 bits of (size - 1)

    # Chunk size field: 14-bit (size - 1), so a chunk carries 1..16384
    # payload bytes. The device-side framer is the floor on what a peer
    # can announce; the field ceiling is the protocol max.
    SIZE_BITS = 14
    MAX_CHUNK = 1 << SIZE_BITS   # 16384

    # Largest chunk we emit. Capped to the device->host FIFO depth so a
    # RESET spray stays short (RESET_SPRAY tracks this); the 14-bit
    # field still tolerates a peer announcing up to MAX_CHUNK.
    MAX_EMIT_CHUNK = 2048

    # RESET spray length. A half-written frame can leave the device
    # parsing a chunk that declares up to MAX_EMIT_CHUNK payload bytes
    # (plus a dangling header byte). Spraying that many RESET bytes
    # overflows the stale payload — the bytes are eaten as payload —
    # so the remainder land on a header boundary and read as RESET.
    # The trailing RELEASE re-arms the parser.
    RESET_SPRAY = MAX_EMIT_CHUNK + 8

    def __init__(self, pipe, name: str = "chunked"):
        super().__init__(name)
        self.__pipe = pipe
        # Futures awaiting a received frame, in arrival order. Serviced
        # by a background reader task so flush_ops never awaits a read.
        self.__recv_waiters: deque = deque()
        self.__reader: asyncio.Task | None = None

    async def start(self):
        await self.__reset()

    async def stop(self):
        if self.__reader is not None:
            self.__reader.cancel()
            self.__reader = None

    async def __reset(self):
        await self.__pipe.write(
            bytes([self.RESET]) * self.RESET_SPRAY + bytes([self.RELEASE]))
        # Stale incoming bytes from a prior session are drained by the
        # transport's open path; nothing to consume here.

    @classmethod
    def __packetize(cls, data: bytes) -> bytes:
        if not data:
            raise ValueError("Chunked cannot carry empty frames")
        view = memoryview(data)
        total = len(view)
        out = bytearray()
        off = 0
        while off < total:
            take = min(cls.MAX_EMIT_CHUNK, total - off)
            last = off + take >= total
            n = take - 1
            b0 = (n >> 8) & cls.SIZE_HI_MASK
            if last:
                b0 |= cls.LAST_FLAG
            out.append(b0)
            out.append(n & 0xff)
            out += view[off:off + take]
            off += take
        return bytes(out)

    async def __read_exact(self, n: int) -> bytes:
        data = await self.__pipe.read(n)
        if len(data) != n:
            raise RuntimeError(f"Chunked: short read ({len(data)}/{n})")
        return data

    async def __recv_one(self) -> bytes:
        frame = bytearray()
        while True:
            b0 = (await self.__read_exact(1))[0]
            if b0 & self.CTRL_FLAG:
                # Stray control marker (a device-side RESET/RELEASE).
                # The framer doesn't emit these in normal operation;
                # tolerate them by dropping any partial reassembly.
                frame.clear()
                continue
            b1 = (await self.__read_exact(1))[0]
            size = (((b0 & self.SIZE_HI_MASK) << 8) | b1) + 1
            frame += await self.__read_exact(size)
            if b0 & self.LAST_FLAG:
                return bytes(frame)

    async def flush_ops(self, batch):
        # Non-blocking (see Batcher contract): post every write to the
        # pipe and chain its future; enqueue every receive for the
        # background reader. No IO is awaited here, so the flush lock is
        # released immediately and writes pipeline with reads on the
        # full-duplex pipe instead of a big write being handed down with
        # no read behind it.
        for op, future in batch:
            if isinstance(op, Send):
                wf = self.__pipe.write(self.__packetize(op.data))
                chain_future(wf, future, lambda _r: None)
            elif isinstance(op, Recv):
                self.__recv_waiters.append(future)
        if self.__recv_waiters:
            self.__ensure_reader()

    def __ensure_reader(self):
        if self.__reader is None or self.__reader.done():
            self.__reader = asyncio.ensure_future(self.__reader_loop())

    async def __reader_loop(self):
        # Services queued receive waiters one frame at a time. Awaiting
        # reads here is fine — this is a background task, not flush_ops.
        while self.__recv_waiters:
            future = self.__recv_waiters.popleft()
            try:
                frame = await self.__recv_one()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:   # noqa: BLE001 — forward to future
                if future is not None and not future.done():
                    future.set_exception(exc)
                continue
            if future is not None and not future.done():
                future.set_result((frame, None))
