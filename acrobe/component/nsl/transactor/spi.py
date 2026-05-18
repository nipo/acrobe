"""NSL SPI transactor batch codec.

Encodes :mod:`acrobe.protocol.spi` Cs/Shift operations into NSL-SPI
transactor command byte streams and decodes the response back into op
results. Pure codec: no transport, no Batcher. The adapter-side
``SpiInterface`` subclass owns the transport and wires the codec to
it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ....protocol.spi import Cs, Shift


class SpiTransactor:
    """Batch codec for the NSL SPI transactor command stream."""

    # Command bit layout (matches RTL nsl_spi_transactor):
    #   0xxx_xxxx  SELECT       cpol[4], cpha[3], slave_id[2:0] (7 = UNSELECT)
    #   001x_xxxx  SET_DIVISOR  divisor[4:0]
    #   01xx_xxxx  SHIFT_IN     count[5:0]+1 (read-only)
    #   10xx_xxxx  SHIFT_OUT    count[5:0]+1 (write-only); host appends mosi
    #   11xx_xxxx  SHIFT_INOUT  count[5:0]+1 (full duplex); host appends mosi
    CMD_SELECT       = 0x00
    UNSELECT_ID      = 0x07
    CMD_DIVISOR      = 0x20
    CMD_SHIFT_IN     = 0x40
    CMD_SHIFT_OUT    = 0x80
    CMD_SHIFT_INOUT  = 0xC0

    MAX_CHUNK = 0x40  # 64 bytes per shift command

    @dataclass(frozen=True, slots=True)
    class __Gather:
        op_idx: int
        rsp_offset: int
        length: int

    def __init__(self, base_freq: float, *, max_chunk: int | None = None):
        self.base_freq = float(base_freq)
        if max_chunk is not None:
            self.max_chunk = int(max_chunk)
        else:
            self.max_chunk = self.MAX_CHUNK
        self.__divisor = max(0, int(self.base_freq / 1e6) - 1) & 0x1f
        self.__rate_dirty = True

    def freq_update(self, freq: float | None) -> float:
        if not freq:
            return self.base_freq / ((self.__divisor + 1) * 2)
        d = int(math.ceil(self.base_freq / 2.0 / float(freq))) - 1
        d = max(0, min(d, 0x1f))
        if d != self.__divisor:
            self.__divisor = d
            self.__rate_dirty = True
        return self.base_freq / ((self.__divisor + 1) * 2)

    def context_force_refresh(self) -> None:
        self.__rate_dirty = True

    def encode(self, batch) -> tuple[bytes, int, list]:
        cmd = bytearray()
        rsp_size = 0
        gather: list = []
        mode = 0

        if self.__rate_dirty:
            cmd.append(self.CMD_DIVISOR | self.__divisor)
            rsp_size += 1
            self.__rate_dirty = False

        for op_idx, (op, _future) in enumerate(batch):
            if isinstance(op, Cs):
                if op.value is not None:
                    mode = op.mode
                    cpol = (mode >> 1) & 1
                    cpha = mode & 1
                    cmd.append(self.CMD_SELECT
                               | (cpol << 4) | (cpha << 3)
                               | (op.value & 0x7))
                else:
                    cpol = (mode >> 1) & 1
                    cpha = mode & 1
                    cmd.append(self.CMD_SELECT
                               | (cpol << 4) | (cpha << 3)
                               | self.UNSELECT_ID)
                rsp_size += 1
                continue

            if isinstance(op, Shift):
                mosi_bytes = bytes(op.mosi)
                if op.read_miso:
                    base_cmd = self.CMD_SHIFT_INOUT
                else:
                    base_cmd = self.CMD_SHIFT_OUT
                if not mosi_bytes:
                    continue
                for off in range(0, len(mosi_bytes), self.max_chunk):
                    chunk = mosi_bytes[off:off + self.max_chunk]
                    cmd.append(base_cmd | (len(chunk) - 1))
                    cmd.extend(chunk)
                    rsp_size += 1
                    if op.read_miso:
                        gather.append(self.__Gather(op_idx, rsp_size,
                                                    len(chunk)))
                        rsp_size += len(chunk)
                continue

            raise TypeError(f"SpiTransactor cannot encode {type(op).__name__}")

        return bytes(cmd), rsp_size, gather

    def decode(self, batch, response: bytes, gather) -> None:
        per_op: dict[int, bytearray] = {}
        for g in gather:
            per_op.setdefault(g.op_idx, bytearray()).extend(
                response[g.rsp_offset:g.rsp_offset + g.length])

        for op_idx, (op, future) in enumerate(batch):
            if isinstance(op, Shift) and op.read_miso:
                miso = bytes(per_op.get(op_idx, b""))
                op.miso = miso
                if future is not None and not future.done():
                    future.set_result(miso)
                continue

            if isinstance(op, Shift):
                op.miso = None
            if future is not None and not future.done():
                future.set_result(None)
