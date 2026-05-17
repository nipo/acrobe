"""NSL JTAG transactor batch codec.

Encodes :mod:`acrobe.protocol.jtag` bit-level operations into
NSL-JTAG transactor command byte streams and decodes the
corresponding response byte streams back into op results. Pure
codec: no transport, no Batcher. The adapter-side
``JtagInterface`` subclass owns the transport and wires the codec to
it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ....bitstring import BitString, BitStringBase
from ....protocol import jtag


class JtagTransactor:
    """Batch codec for the NSL JTAG transactor command stream."""

    # Command bit layout (matches firmware constants):
    #   0xxx_xxxx  SHIFT_BYTE   bits[6]=W, bits[5]=R, bits[4:0]=count-1
    #   111x_xxxx  SHIFT_BIT    bits[4]=W, bits[3]=R, bits[2:0]=count-1
    #   1001_1xxx  RESET        cycles[2:0]+1
    #   1001_0xxx  RTI          cycles[2:0]+1
    #   1011_xxxx  RESET8       (cycles[3:0]+1) * 8
    #   1010_xxxx  RTI8         (cycles[3:0]+1) * 8
    #   1000_0000  DR_CAPTURE
    #   1000_0001  IR_CAPTURE
    #   1000_0010  SWD_TO_JTAG
    #   1000_0011  DIVISOR      host appends 1 byte (8-bit divisor-1)
    #   1000_010x  SYS_RESET    bit[0]=assert
    CMD_SHIFT_BYTE   = 0x00
    CMD_SHIFT_BYTE_W = 0x40
    CMD_SHIFT_BYTE_R = 0x20
    CMD_SHIFT_BIT    = 0xe0
    CMD_SHIFT_BIT_W  = 0x10
    CMD_SHIFT_BIT_R  = 0x08
    CMD_DR_CAPTURE   = 0x80
    CMD_IR_CAPTURE   = 0x81
    CMD_SWD_TO_JTAG  = 0x82
    CMD_DIVISOR      = 0x83
    CMD_SYS_RESET    = 0x84
    CMD_RTI          = 0x90
    CMD_RESET        = 0x98
    CMD_RTI8         = 0xa0
    CMD_RESET8       = 0xb0

    @dataclass(frozen=True, slots=True)
    class __Gather:
        """One TDO-bearing command's response slot. Used to reassemble
        a (potentially chunked) Shift op's TDO output."""

        op_idx: int
        rsp_offset: int          # offset of data bytes (after status byte)
        bit_count: int           # bits read from this chunk

    def __init__(self, base_freq: float, *, max_chunk: int = 1024):
        self.base_freq = float(base_freq)
        self.max_chunk = max_chunk
        d = max(0, int(self.base_freq / 1e6) - 1)
        self.__divisor = min(d, 0xff)
        self.__rate_dirty = True

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def freq_update(self, freq: float | None) -> float:
        """Recompute divisor for `freq`. Returns the actual achievable
        TCK frequency."""
        if not freq:
            return self.base_freq / ((self.__divisor + 1) * 2)
        d = math.ceil(self.base_freq / 2.0 / float(freq)) - 1
        d = max(0, min(d, 0xff))
        if d != self.__divisor:
            self.__divisor = d
            self.__rate_dirty = True
        return self.base_freq / ((self.__divisor + 1) * 2)

    def context_force_refresh(self) -> None:
        self.__rate_dirty = True

    # ------------------------------------------------------------------
    # Batch codec
    # ------------------------------------------------------------------

    def encode(self, batch) -> tuple[bytes, int, list]:
        cmd = bytearray()
        rsp_size = 0
        gather: list = []

        if self.__rate_dirty:
            cmd.append(self.CMD_DIVISOR)
            cmd.append(self.__divisor & 0xff)
            rsp_size += 1
            self.__rate_dirty = False

        for op_idx, (op, _future) in enumerate(batch):
            rsp_size = self.__encode_one(op, op_idx, cmd, rsp_size, gather)

        return bytes(cmd), rsp_size, gather

    def __encode_one(self, op, op_idx, cmd, rsp_size, gather):
        if isinstance(op, jtag.CaptureDr):
            cmd.append(self.CMD_DR_CAPTURE)
            rsp_size += 1
            return rsp_size

        if isinstance(op, jtag.CaptureIr):
            cmd.append(self.CMD_IR_CAPTURE)
            rsp_size += 1
            return rsp_size

        if isinstance(op, jtag.SwdToJtag):
            cmd.append(self.CMD_SWD_TO_JTAG)
            rsp_size += 1
            return rsp_size

        if isinstance(op, jtag.Reset):
            return self.__encode_reset(op.count, cmd, rsp_size)

        if isinstance(op, jtag.Run):
            return self.__encode_rti(op.cycles, cmd, rsp_size)

        if isinstance(op, jtag.Shift):
            rsp_size = self.__encode_shift(op, op_idx, cmd, rsp_size, gather)
            if op.post_run:
                rsp_size = self.__encode_rti(op.post_run, cmd, rsp_size)
            return rsp_size

        raise TypeError(f"JtagTransactor cannot encode {type(op).__name__}")

    def __encode_reset(self, count: int, cmd, rsp_size):
        cycles = max(5, int(count))
        # Use RESET8 (groups of 8 cycles) for the bulk, then fill with
        # RESET for the remainder. RESET8 count field is 4 bits =
        # 1..16 groups of 8 cycles per command.
        while cycles >= 8:
            packs = cycles // 8
            n = min(packs, 16)
            cmd.append(self.CMD_RESET8 | (n - 1))
            rsp_size += 1
            cycles -= n * 8
        if cycles:
            cmd.append(self.CMD_RESET | (cycles - 1))
            rsp_size += 1
        return rsp_size

    def __encode_rti(self, cycles: int, cmd, rsp_size):
        cycles = int(cycles)
        if cycles <= 0:
            return rsp_size
        while cycles >= 8:
            packs = cycles // 8
            n = min(packs, 16)
            cmd.append(self.CMD_RTI8 | (n - 1))
            rsp_size += 1
            cycles -= n * 8
        if cycles:
            cmd.append(self.CMD_RTI | (cycles - 1))
            rsp_size += 1
        return rsp_size

    def __encode_shift(self, op, op_idx, cmd, rsp_size, gather):
        # tdi must be a BitString (acrobe's JTAG always uses one).
        tdi = op.tdi
        if not isinstance(tdi, BitStringBase):
            raise TypeError(
                f"jtag.Shift.tdi must be BitString, got {type(tdi).__name__}")

        has_tdi = True  # NSL transactor always supports W
        has_tdo = bool(op.read_tdo)

        shift_byte_cmd = self.CMD_SHIFT_BYTE
        shift_bit_cmd = self.CMD_SHIFT_BIT
        if has_tdi:
            shift_byte_cmd |= self.CMD_SHIFT_BYTE_W
            shift_bit_cmd |= self.CMD_SHIFT_BIT_W
        if has_tdo:
            shift_byte_cmd |= self.CMD_SHIFT_BYTE_R
            shift_bit_cmd |= self.CMD_SHIFT_BIT_R

        bit_count = len(tdi)
        data = bytes(tdi)

        # Response order for shift commands is *data first, then
        # status byte* — opposite of SWD. Firmware confirms in
        # transactor/jtag/jtag_transactor.c handler: PUT_DATA runs
        # when trn->left > 0, then PUT_RSP.
        while bit_count >= 8:
            byte_count = min(bit_count // 8, 32)
            cmd.append(shift_byte_cmd | (byte_count - 1))
            cmd.extend(data[:byte_count])
            data = data[byte_count:]
            if has_tdo:
                gather.append(self.__Gather(op_idx, rsp_size, byte_count * 8))
                rsp_size += byte_count
            rsp_size += 1  # status byte (at the end of this command's response)
            bit_count -= byte_count * 8

        if bit_count:
            cmd.append(shift_bit_cmd | (bit_count - 1))
            cmd.append(data[0] if data else 0)
            if has_tdo:
                gather.append(self.__Gather(op_idx, rsp_size, bit_count))
                rsp_size += 1  # 1 byte holds the tail TDO bits
            rsp_size += 1  # status byte (at the end)

        return rsp_size

    # ------------------------------------------------------------------
    # Decode
    # ------------------------------------------------------------------

    def decode(self, batch, response: bytes, gather) -> None:
        # Reassemble per-op TDO BitStrings from the gather records.
        by_op: dict[int, BitString] = {}
        for g in gather:
            chunk = BitString(response[g.rsp_offset:
                                       g.rsp_offset + (g.bit_count + 7) // 8],
                              g.bit_count)
            prev = by_op.get(g.op_idx)
            by_op[g.op_idx] = chunk if prev is None else prev + chunk

        for op_idx, (op, future) in enumerate(batch):
            if future is None or future.done():
                continue
            if isinstance(op, jtag.Shift) and op.read_tdo:
                future.set_result(by_op.get(op_idx, BitString()))
            else:
                future.set_result(None)
