import math

from ....engine import Batcher
from ....node import Node
from ....protocol.spi import Cs, Shift
from ..bnoc.framed import Framed


class SpiTransactor(Batcher, Node):
    """Encodes spi.Cs/Shift ops as NSL SPI transactor commands.

    Command encoding (matches RTL nsl_spi_transactor):
    - 0x00 | cpol<<4 | cpha<<3 | slave_id  — SELECT (slave 0-6)
    - 0x00 | cpol<<4 | cpha<<3 | 7         — UNSELECT (slave_id = 7)
    - 0x20 | divisor                        — SET_DIVISOR (5-bit)
    - 0x80 | (count-1)                      — SHIFT_OUT (write-only), + mosi bytes
    - 0x40 | (count-1)                      — SHIFT_IN (read-only)
    - 0xC0 | (count-1)                      — SHIFT_INOUT (full-duplex), + mosi bytes
    Max 64 bytes per shift chunk.

    Response: 1 status byte per command + MISO bytes for IN/INOUT.
    """

    CMD_SELECT = 0x00
    CMD_UNSELECT = 0x07
    CMD_DIVISOR = 0x20
    CMD_SHIFT_IN = 0x40
    CMD_SHIFT_OUT = 0x80
    CMD_SHIFT_INOUT = 0xC0

    MAX_CHUNK = 0x40  # 64 bytes

    def __init__(self, channel: Framed, base_freq: float,
                 name: str = "spi-xact"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self._channel = channel
        self.base_freq = base_freq
        self._divisor = max(0, int(base_freq / 1e6) - 1) & 0x1f
        self._rate_dirty = True

    def freq_update(self, freq) -> float:
        """Compute divisor for target frequency. Returns actual frequency."""
        if not freq:
            freq = 15e6
        divisor = int(math.ceil(self.base_freq / 2.0 / float(freq))) - 1
        divisor = max(0, min(divisor, 0x1f))
        if self._divisor != divisor:
            self._divisor = divisor
            self._rate_dirty = True
        return self.base_freq / ((self._divisor + 1) * 2)

    async def flush_ops(self, batch):
        cmd = bytearray()
        rsp_size = 0
        mode = 0

        # Per-shift gather info: list of (response_offset, length) tuples
        gather_map = []  # (batch_idx, gather_list)

        if self._rate_dirty:
            cmd.append(self.CMD_DIVISOR | self._divisor)
            rsp_size += 1
            self._rate_dirty = False

        for idx, (op, future) in enumerate(batch):
            if isinstance(op, Cs):
                if op.value is not None:
                    mode = op.mode
                    cpol = (mode >> 1) & 1
                    cpha = mode & 1
                    cmd.append(self.CMD_SELECT | (cpol << 4) | (cpha << 3) | op.value)
                else:
                    cpol = (mode >> 1) & 1
                    cpha = mode & 1
                    cmd.append(self.CMD_UNSELECT | (cpol << 4) | (cpha << 3))
                rsp_size += 1

            elif isinstance(op, Shift):
                mosi_bytes = bytes(op.mosi)
                gather = []

                if op.read_miso:
                    # Read or full-duplex: SHIFT_INOUT with mosi data
                    for off in range(0, len(mosi_bytes), self.MAX_CHUNK):
                        left = min(self.MAX_CHUNK, len(mosi_bytes) - off)
                        cmd.append(self.CMD_SHIFT_INOUT | (left - 1))
                        cmd.extend(mosi_bytes[off:off + left])
                        rsp_size += 1  # status byte
                        gather.append((rsp_size, left))
                        rsp_size += left
                else:
                    # Write-only
                    for off in range(0, len(mosi_bytes), self.MAX_CHUNK):
                        left = min(self.MAX_CHUNK, len(mosi_bytes) - off)
                        cmd.append(self.CMD_SHIFT_OUT | (left - 1))
                        cmd.extend(mosi_bytes[off:off + left])
                        rsp_size += 1  # status byte

                gather_map.append((idx, gather))

        # Send command frame and receive response
        self._channel.send(bytes(cmd))
        response, _ = await self._channel.recv()

        # Parse response: populate miso from gather info
        for idx, gather in gather_map:
            op = batch[idx][0]
            if gather:
                op.miso = b''.join(
                    response[off:off + size] for off, size in gather)
            else:
                op.miso = None

        # Resolve all futures
        for op, future in batch:
            future.set_result(op)
