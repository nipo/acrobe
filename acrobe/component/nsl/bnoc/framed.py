import asyncio

from ....engine import Batcher


class FrameSend:
    def __init__(self, data: bytes):
        self.data = data

    def __repr__(self):
        return f"<FrameSend {len(self.data)}B>"


class FrameRecv:
    def __init__(self):
        self.data = None

    def __repr__(self):
        return "<FrameRecv>"


class Framed(Batcher):
    """Base class for framed communication.
    Matches RTL nsl_bnoc_framed: data stream with packet boundaries.
    """

    LAST = 0x100  # bit 8 of 9-bit word

    def send(self, data: bytes) -> asyncio.Future:
        return self.post(FrameSend(data))

    def recv(self) -> asyncio.Future:
        return self.post(FrameRecv())

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
    """Concrete framed implementation over JTAG FIFO."""

    def __init__(self, fifo):
        super().__init__()
        self._fifo = fifo

    async def flush_ops(self, batch):
        sends = [(op, f) for op, f in batch if isinstance(op, FrameSend)]
        recvs = [(op, f) for op, f in batch if isinstance(op, FrameRecv)]

        # Encode all sends as 9-bit words
        tx_words = []
        for op, _ in sends:
            tx_words.extend(self.encode(op.data))

        # Exchange: send all, receive expect_frames complete frames
        rx_words = await self._fifo.exchange(
            tx_words, expect_frames=len(recvs))

        # Split rx_words into frames at LAST markers
        frames = self.split_frames(rx_words)

        # Resolve send futures
        for op, future in sends:
            future.set_result(op)

        # Resolve recv futures
        for (op, future), frame_words in zip(recvs, frames):
            op.data = self.decode(frame_words)
            future.set_result(op)
