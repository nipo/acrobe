"""NSL bnoc sized framing over a Pipe.

Matches RTL ``nsl_bnoc.sized``: a sized channel carries discrete
frames over an unframed byte pipe by prefixing every payload with a
16-bit little-endian size header, off-by-one (header ``0x0000`` →
1-byte payload). The framer treats a header of ``0xFFFF`` as a
sentinel that drains everything until a ``0x00`` byte arrives.

The Python `Sized` class wraps a :class:`~acrobe.protocol.pipe.Pipe`
and presents a :class:`~acrobe.protocol.datagram.Datagram` interface
to its users. On start it sends a reset sequence (a long burst of
``0xFF`` bytes followed by a single ``0x00``) so the device-side
framer is in a known state before any real traffic flows.
"""

from __future__ import annotations

import asyncio
from collections import deque

from ....protocol.datagram import Datagram, Send, Recv
from ....protocol.pipe import Pipe

@Pipe.db.register("sized")
class Sized(Datagram):
    """Datagram over a `Pipe` backend with per-frame size headers."""

    # Reset burst length. 1023 bytes of 0xFF guarantee the device-side
    # framer reads a size header of 0xFFFF (the INVAL sentinel) no
    # matter what state the byte stream was in, then the trailing
    # 0x00 exits the INVAL state on the device side.
    RESET_FF_COUNT = 1023

    # Maximum frame payload (matches the FPGA ``sized_from_framed``
    # default ``max_txn_length``). Used as a sanity bound on incoming
    # size headers.
    MAX_FRAME_SIZE = 0x10000

    def __init__(self, pipe, name: str = "sized"):
        super().__init__(name)
        self.__pipe = pipe
        self.__rx_queue: deque[bytes] = deque()

    async def start(self):
        await self.__reset()

    async def __reset(self):
        # Burst 0xFF for the host-to-device direction so the framer
        # treats whatever's in flight as a 0xFFFF (INVAL) header and
        # discards it. The trailing 0x00 byte exits INVAL state.
        await self.__pipe.write(bytes([0xff] * self.RESET_FF_COUNT + [0x00]))
        # Stale incoming bytes from a prior session are drained by
        # the transport's open path; nothing to consume here.
        self.__rx_queue.clear()

    @classmethod
    def __packetize(cls, data: bytes) -> bytes:
        if not data:
            raise ValueError("Sized cannot carry empty frames")
        size = len(data) - 1
        if size > 0xfffe:
            raise ValueError(
                f"Sized frame too large: {len(data)} > {0xffff} bytes")
        return size.to_bytes(2, "little") + data

    async def __recv_one(self) -> bytes:
        # Read exactly 2 bytes for the size header, then exactly
        # (size+1) bytes for the payload.
        header = await self.__pipe.read(2)
        if len(header) != 2:
            raise RuntimeError(
                f"Sized: short read on size header ({len(header)}/2)")
        size = int.from_bytes(header, "little") + 1
        if size > self.MAX_FRAME_SIZE:
            raise RuntimeError(
                f"Sized: oversized frame announced ({size} > "
                f"{self.MAX_FRAME_SIZE})")
        payload = await self.__pipe.read(size)
        if len(payload) != size:
            raise RuntimeError(
                f"Sized: short read on payload ({len(payload)}/{size})")
        return bytes(payload)

    async def flush_ops(self, batch):
        # Group sends first so the wire sees the request burst before
        # we start trying to read responses.
        sends = [(op, f) for op, f in batch if isinstance(op, Send)]
        recvs = [(op, f) for op, f in batch if isinstance(op, Recv)]

        send_futures = []
        for op, future in sends:
            wf = self.__pipe.write(self.__packetize(op.data))
            send_futures.append((wf, future))

        if send_futures:
            await asyncio.gather(*[wf for wf, _ in send_futures])
            for _, mf in send_futures:
                if mf is not None and not mf.done():
                    mf.set_result(None)

        for op, future in recvs:
            if self.__rx_queue:
                data = self.__rx_queue.popleft()
            else:
                data = await self.__recv_one()
            if future is None or future.done():
                continue
            future.set_result((data, None))
