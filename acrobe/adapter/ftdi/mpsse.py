from __future__ import annotations

import asyncio
from typing import Protocol

from . import mpsse_cmd
from ...bitstring import BitString, BitStringBase
from ...engine import Batcher


class Transport(Protocol):
    def write(self, data: bytes) -> asyncio.Future: ...
    def read(self, byte_count: int) -> asyncio.Future: ...
    async def transfer(self, data: bytes, byte_count: int) -> bytes: ...


class Operation:
    def cmd_data(self) -> tuple[bytes, int, float]:
        """Returns (command_bytes, response_byte_count, cycle_count)."""
        return b"", 0, 0.0

    def rsp_handle(self, data: bytes):
        """Process response data. Called by engine after USB read."""
        pass

    def __repr__(self):
        return f"<{self.__class__.__name__}>"


# --- GPIO operations ---

class SetBitsLow(Operation):
    def __init__(self, value: int, oe: int):
        self.value = value
        self.oe = oe

    def cmd_data(self):
        return bytes([mpsse_cmd.SET_BITS_LOW, self.value, self.oe]), 0, 1

    def __repr__(self):
        return f"<SetBitsLow {self.value:#04x}/{self.oe:#04x}>"


class SetBitsHigh(Operation):
    def __init__(self, value: int, oe: int):
        self.value = value
        self.oe = oe

    def cmd_data(self):
        return bytes([mpsse_cmd.SET_BITS_HIGH, self.value, self.oe]), 0, 1

    def __repr__(self):
        return f"<SetBitsHigh {self.value:#04x}/{self.oe:#04x}>"


class GetBitsLow(Operation):
    def __init__(self):
        self.value = 0

    def cmd_data(self):
        return bytes([mpsse_cmd.GET_BITS_LOW]), 1, 1

    def rsp_handle(self, data: bytes):
        self.value = data[0]

    def __repr__(self):
        return "<GetBitsLow>"


class GetBitsHigh(Operation):
    def __init__(self):
        self.value = 0

    def cmd_data(self):
        return bytes([mpsse_cmd.GET_BITS_HIGH]), 1, 1

    def rsp_handle(self, data: bytes):
        self.value = data[0]

    def __repr__(self):
        return "<GetBitsHigh>"


# --- Configuration operations ---

class Loopback(Operation):
    def __init__(self, enable: bool):
        self.enable = enable

    def cmd_data(self):
        cmd = mpsse_cmd.LOOPBACK_ENABLE if self.enable else mpsse_cmd.LOOPBACK_DISABLE
        return bytes([cmd]), 0, 0

    def __repr__(self):
        return f"<Loopback {'on' if self.enable else 'off'}>"


class ClockDivisor(Operation):
    def __init__(self, divisor: int):
        self.divisor = divisor

    def cmd_data(self):
        d = self.divisor - 1
        return bytes([mpsse_cmd.CLK_DIV, d & 0xff, d >> 8]), 0, 0

    def __repr__(self):
        return f"<ClockDivisor {self.divisor}>"


class ClockDiv5(Operation):
    def __init__(self, enable: bool):
        self.enable = enable

    def cmd_data(self):
        cmd = mpsse_cmd.CLK_DIV5_ENABLE if self.enable else mpsse_cmd.CLK_DIV5_DISABLE
        return bytes([cmd]), 0, 0

    def __repr__(self):
        return f"<ClockDiv5 {'on' if self.enable else 'off'}>"


class ThreePhase(Operation):
    def __init__(self, enable: bool):
        self.enable = enable

    def cmd_data(self):
        cmd = mpsse_cmd.THREE_PHASE_ENABLE if self.enable else mpsse_cmd.THREE_PHASE_DISABLE
        return bytes([cmd]), 0, 0

    def __repr__(self):
        return f"<ThreePhase {'on' if self.enable else 'off'}>"


class Adaptive(Operation):
    def __init__(self, enable: bool):
        self.enable = enable

    def cmd_data(self):
        cmd = mpsse_cmd.ADAPTIVE_ENABLE if self.enable else mpsse_cmd.ADAPTIVE_DISABLE
        return bytes([cmd]), 0, 0

    def __repr__(self):
        return f"<Adaptive {'on' if self.enable else 'off'}>"


class SendImmediate(Operation):
    def cmd_data(self):
        return bytes([mpsse_cmd.SEND_IMMEDIATE]), 0, 0


class WaitOnHigh(Operation):
    def cmd_data(self):
        return bytes([mpsse_cmd.WAIT_ON_HIGH]), 0, 0


class WaitOnLow(Operation):
    def cmd_data(self):
        return bytes([mpsse_cmd.WAIT_ON_LOW]), 0, 0


class ClockBits(Operation):
    def __init__(self, count: int):
        assert 1 <= count <= 8
        self.count = count

    def cmd_data(self):
        return bytes([mpsse_cmd.CLK_BITS, self.count - 1]), 0, self.count

    def __repr__(self):
        return f"<ClockBits {self.count}>"


class ClockBytes(Operation):
    def __init__(self, count: int):
        assert 1 <= count <= 65536
        self.count = count

    def cmd_data(self):
        c = self.count - 1
        return bytes([mpsse_cmd.CLK_BYTES, c & 0xff, c >> 8]), 0, self.count

    def __repr__(self):
        return f"<ClockBytes {self.count}>"


# --- Shift operations ---

class ShiftBits(Operation):
    def __init__(self, data, count: int, write_pol: str = "-",
                 read_pol: str = "+", lsb_first: bool = True,
                 read: bool = False):
        if not (1 <= count <= 8):
            raise ValueError(f"Shifting too many bits: {count}")

        self.lsb_first = lsb_first
        self.count = count
        self.read = read

        cmd = mpsse_cmd.BITS

        data_out = b""
        if lsb_first:
            cmd |= mpsse_cmd.LSB

        if write_pol != "+":
            cmd |= mpsse_cmd.WRITE_NEG

        if data is not None:
            cmd |= mpsse_cmd.WRITE
            data = data or 0
            if not lsb_first:
                data <<= 8 - count
            data_out = bytes([data])

        if read:
            cmd |= mpsse_cmd.READ
            if read_pol != "+":
                cmd |= mpsse_cmd.READ_NEG

        self.cmd = bytes([cmd, count - 1]) + data_out

    def cmd_data(self):
        return self.cmd, 1 if self.read else 0, self.count

    def rsp_handle(self, data: bytes):
        if self.read:
            if self.lsb_first:
                self.data = BitString(data[0] >> (8 - self.count), self.count)
            else:
                self.data = BitString(data[0], self.count)
        else:
            self.data = None

    def __repr__(self):
        return f"<ShiftBits {self.count}>"


class ShiftBytes(Operation):
    def __init__(self, data_or_bytecnt, write_pol: str = "-",
                 read_pol: str = "+", lsb_first: bool = True,
                 read: bool = False):
        cmd = 0
        self.rt = bytes

        if isinstance(data_or_bytecnt, int):
            byte_count = data_or_bytecnt
            data_out = b""
        elif isinstance(data_or_bytecnt, BitStringBase):
            assert (len(data_or_bytecnt) % 8) == 0
            data_out = bytes(data_or_bytecnt)
            if not lsb_first:
                data_out = data_out[::-1]
            byte_count = len(data_out)
            cmd |= mpsse_cmd.WRITE
            self.rt = BitString
        else:
            assert isinstance(data_or_bytecnt, (bytes, bytearray))
            data_out = bytes(data_or_bytecnt)
            byte_count = len(data_out)
            cmd |= mpsse_cmd.WRITE

        if write_pol != "+":
            cmd |= mpsse_cmd.WRITE_NEG

        if not (1 <= byte_count <= 65536):
            raise ValueError(f"Shifting too many bytes: {byte_count}")

        self.lsb_first = lsb_first
        if lsb_first:
            cmd |= mpsse_cmd.LSB
        if read:
            cmd |= mpsse_cmd.READ
            if read_pol != "+":
                cmd |= mpsse_cmd.READ_NEG
            self.read_byte_count = byte_count
        else:
            self.read_byte_count = 0

        self.cmd = bytes([cmd]) + (byte_count - 1).to_bytes(2, "little") + data_out
        self.byte_count = byte_count
        self.read = read

    def cmd_data(self):
        return self.cmd, self.read_byte_count, self.byte_count * 8

    def rsp_handle(self, data: bytes):
        if self.read:
            if self.rt is bytes:
                self.data = data
            else:
                blob = data
                if not self.lsb_first:
                    blob = blob[::-1]
                self.data = BitString(blob, self.byte_count * 8)
        else:
            self.data = None

    def __repr__(self):
        return f"<ShiftBytes {self.byte_count}x8>"


class ShiftTms(Operation):
    def __init__(self, data: int, count: int, write_pol: str = "-",
                 read_pol: str = "+", read: bool = False, tdi: int = 0):
        if not (1 <= count <= 8):
            raise ValueError(f"Shifting too many bits: {count}")

        cmd = mpsse_cmd.BITS | mpsse_cmd.TMS | mpsse_cmd.LSB

        if write_pol != "+":
            cmd |= mpsse_cmd.WRITE_NEG
        if read:
            cmd |= mpsse_cmd.READ
            if read_pol != "+":
                cmd |= mpsse_cmd.READ_NEG

        self.cmd = bytes([cmd, count - 1, (data & 0x7f) | ((tdi & 1) << 7)])
        self.count = count
        self.write_pol = write_pol
        self.read_pol = read_pol
        self.read = read
        self.tms = BitString(data, count)
        self.tdi = int(bool(tdi))

    def cmd_data(self):
        return self.cmd, 1 if self.read else 0, self.count

    def rsp_handle(self, data: bytes):
        if self.read:
            self.data = BitString(data[0] >> (8 - self.count), self.count)
        else:
            self.data = None

    def __repr__(self):
        return f"<ShiftTms tms={str(self.tms)} tdi={self.tdi}>"

    def __add__(self, other):
        assert isinstance(other, ShiftTms)
        tms = self.tms + other.tms
        return ShiftTms(int(tms), len(tms),
                        write_pol=self.write_pol,
                        read_pol=self.read_pol,
                        read=self.read,
                        tdi=other.tdi)


# --- Engine ---

class MpsseEngine(Batcher):
    def __init__(self, transport: Transport, logger):
        super().__init__()
        self._transport = transport
        self.logger = logger
        self._bracket_pre = b""
        self._bracket_post = b""

    def set_bracket(self, pre: bytes, post: bytes):
        """Raw MPSSE bytes prepended/appended to each batch's command stream.

        Both sequences must produce zero response bytes. Used to pulse
        activity LEDs or similar per-batch side-effects.
        """
        self._bracket_pre = pre
        self._bracket_post = post

    async def flush_ops(self, batch):
        cmd_parts = []
        rsp_ranges = []
        total_rsp = 0

        if self._bracket_pre:
            cmd_parts.append(self._bracket_pre)

        for op, future in batch:
            cmd, rsp_len, _ = op.cmd_data()
            cmd_parts.append(cmd)
            rsp_ranges.append((total_rsp, total_rsp + rsp_len))
            total_rsp += rsp_len

        if self._bracket_post:
            cmd_parts.append(self._bracket_post)

        if total_rsp == 0:
            # Need at least 1 response byte to synchronize
            cmd_parts.append(bytes([mpsse_cmd.GET_BITS_LOW]))
            total_rsp = 1
        cmd_parts.append(bytes([mpsse_cmd.SEND_IMMEDIATE]))

        cmd = b"".join(cmd_parts)
        self.logger.protocol("USB >> %d bytes, expect %d back", len(cmd), total_rsp)
        self._transport.write(cmd)

        def read_done(rsp):
            try:
                data = rsp.result()
            except BaseException as exc:
                # USB failure: every batch future would otherwise dangle,
                # which deadlocks upper layers chained via add_done_callback.
                for op, future in batch:
                    if not future.done():
                        future.set_exception(exc)
                return

            self.logger.protocol("USB << %d bytes", len(data))

            for (op, future), (start, end) in zip(batch, rsp_ranges):
                if start < end:
                    op.rsp_handle(data[start:end])
                future.set_result(op)

        self._transport.read(total_rsp).add_done_callback(read_done)
