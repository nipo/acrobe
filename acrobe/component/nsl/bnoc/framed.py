"""NSL bnoc 9-bit framed FIFO transport over JTAG.

`JtagFramed` is a :class:`acrobe.protocol.datagram.Datagram` that runs
the FPGA-side ``nsl_bnoc.framed`` AXI-stream protocol on top of a
9-bit-word JTAG FIFO (`JtagFifo`). Each frame on the wire is a stream
of 9-bit words; the final word carries a ``LAST`` marker in bit 8.
"""

from __future__ import annotations

from ....protocol.datagram import Datagram, Send, Recv


class JtagFramed(Datagram):
    """Framed channel over a `JtagFifo` (FPGA-side
    ``nsl_bnoc.framed_jtag`` peripheral)."""

    LAST = 0x100  # bit 8 of the 9-bit word

    def __init__(self, fifo, name: str = "framed"):
        super().__init__(name)
        self.__fifo = fifo

    @staticmethod
    def encode(data: bytes) -> list[int]:
        """Encode bytes as 9-bit words with LAST marker on final word."""
        words = list(data)
        if words:
            words[-1] |= JtagFramed.LAST
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
            if w & JtagFramed.LAST:
                frames.append(current)
                current = []
        if current:
            frames.append(current)
        return frames

    async def flush_ops(self, batch):
        sends = [(op, f) for op, f in batch if isinstance(op, Send)]
        recvs = [(op, f) for op, f in batch if isinstance(op, Recv)]

        tx_words = []
        for op, _ in sends:
            tx_words.extend(self.encode(op.data))

        rx_words = await self.__fifo.exchange(
            tx_words, expect_frames=len(recvs))

        frames = self.split_frames(rx_words)

        for op, future in sends:
            if future is not None and not future.done():
                future.set_result(None)

        for (op, future), frame_words in zip(recvs, frames):
            if future is not None and not future.done():
                future.set_result((self.decode(frame_words), None))
