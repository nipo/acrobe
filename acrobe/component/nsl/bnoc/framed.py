"""NSL bnoc 9-bit framed FIFO transport.

`Framed` is the concrete implementation of the abstract
:class:`acrobe.protocol.datagram.Datagram` for transports that carry
discrete frames as a stream of 9-bit words (8-bit data + a ``LAST``
marker on the final word of each frame). This is the FPGA-side
:mod:`nsl_bnoc.framed` AXI-stream protocol.

`JtagFramed` is the leaf implementation that runs the protocol over a
JTAG-based FIFO transport (:class:`JtagFifo`).
"""

from __future__ import annotations

from ....protocol.datagram import Datagram, Send, Recv


class Framed(Datagram):
    """Abstract NSL 9-bit-framed channel base.

    Concrete subclasses (`JtagFramed`, `Committed`, `Route`) implement
    :meth:`flush_ops`. The 9-bit framing helpers below are shared
    between subclasses that have to encode/decode raw wire words.
    """

    LAST = 0x100  # bit 8 of the 9-bit word

    def __init__(self, name: str):
        super().__init__(name)

    @staticmethod
    def encode(data: bytes) -> list[int]:
        """Encode bytes as 9-bit words with LAST marker on final word."""
        words = list(data)
        if words:
            words[-1] |= Framed.LAST
        return words

    @staticmethod
    def decode(words: list[int]) -> bytes:
        """Decode 9-bit words to bytes, stripping LAST markers."""
        return bytes(w & 0xFF for w in words)

    @staticmethod
    def split_frames(words: list[int]) -> list[list[int]]:
        """Split a flat list of 9-bit words into frames at LAST markers."""
        frames = []
        current = []
        for w in words:
            current.append(w)
            if w & Framed.LAST:
                frames.append(current)
                current = []
        if current:
            frames.append(current)
        return frames


class JtagFramed(Framed):
    """Framed channel over a `JtagFifo` (the FPGA-side
    :mod:`nsl_bnoc.framed_jtag` peripheral)."""

    def __init__(self, fifo, name: str = "framed"):
        super().__init__(name)
        self._fifo = fifo

    async def flush_ops(self, batch):
        sends = [(op, f) for op, f in batch if isinstance(op, Send)]
        recvs = [(op, f) for op, f in batch if isinstance(op, Recv)]

        tx_words = []
        for op, _ in sends:
            tx_words.extend(self.encode(op.data))

        rx_words = await self._fifo.exchange(
            tx_words, expect_frames=len(recvs))

        frames = self.split_frames(rx_words)

        for op, future in sends:
            if not future.done():
                future.set_result(None)

        for (op, future), frame_words in zip(recvs, frames):
            if not future.done():
                future.set_result((self.decode(frame_words), None))
