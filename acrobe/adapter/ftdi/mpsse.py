from __future__ import annotations

import asyncio
from typing import Protocol

from . import mpsse_cmd
from ...bitstring import BitString, BitStringBase
from ...engine import Batcher
from ...log import PROTOCOL


class Transport(Protocol):
    def write(self, data: bytes) -> asyncio.Future: ...
    def read(self, byte_count: int) -> asyncio.Future: ...
    async def transfer(self, data: bytes, byte_count: int) -> bytes: ...


class Operation:
    """MPSSE command op.

    Hot path is :py:meth:`encode` — appends the op's command bytes
    directly into a shared bytearray, with no per-op intermediate
    bytes allocation. ``rsp_size`` and ``cycle_count`` are class- or
    instance-attributes inspected by :class:`MpsseEngine` while
    walking the batch.

    :py:meth:`cmd_data` is kept as a backward-compatible accessor
    derived from ``encode`` for tests and out-of-loop callers.
    """

    rsp_size: int = 0
    cycle_count: int = 0

    def encode(self, buf: bytearray) -> None:
        """Append this op's MPSSE command bytes to ``buf``."""
        pass

    def cmd_data(self) -> tuple[bytes, int, float]:
        """Returns (command_bytes, response_byte_count, cycle_count).

        Compatibility shim derived from :py:meth:`encode`. Avoid in
        hot paths — :class:`MpsseEngine` uses ``encode`` directly to
        skip the per-op bytes allocation."""
        buf = bytearray()
        self.encode(buf)
        return bytes(buf), self.rsp_size, self.cycle_count

    def rsp_handle(self, data: bytes):
        """Process response data. Called by engine after USB read."""
        pass

    def __repr__(self):
        return f"<{self.__class__.__name__}>"


# --- GPIO operations ---

class SetBitsLow(Operation):
    cycle_count = 1

    def __init__(self, value: int, oe: int):
        self.value = value
        self.oe = oe

    def encode(self, buf):
        buf.append(mpsse_cmd.SET_BITS_LOW)
        buf.append(self.value)
        buf.append(self.oe)

    def __repr__(self):
        return f"<SetBitsLow {self.value:#04x}/{self.oe:#04x}>"


class SetBitsHigh(Operation):
    cycle_count = 1

    def __init__(self, value: int, oe: int):
        self.value = value
        self.oe = oe

    def encode(self, buf):
        buf.append(mpsse_cmd.SET_BITS_HIGH)
        buf.append(self.value)
        buf.append(self.oe)

    def __repr__(self):
        return f"<SetBitsHigh {self.value:#04x}/{self.oe:#04x}>"


class GetBitsLow(Operation):
    rsp_size = 1
    cycle_count = 1

    def __init__(self):
        self.value = 0

    def encode(self, buf):
        buf.append(mpsse_cmd.GET_BITS_LOW)

    def rsp_handle(self, data: bytes):
        self.value = data[0]

    def __repr__(self):
        return "<GetBitsLow>"


class GetBitsHigh(Operation):
    rsp_size = 1
    cycle_count = 1

    def __init__(self):
        self.value = 0

    def encode(self, buf):
        buf.append(mpsse_cmd.GET_BITS_HIGH)

    def rsp_handle(self, data: bytes):
        self.value = data[0]

    def __repr__(self):
        return "<GetBitsHigh>"


# --- Configuration operations ---

class Loopback(Operation):
    def __init__(self, enable: bool):
        self.enable = enable

    def encode(self, buf):
        buf.append(mpsse_cmd.LOOPBACK_ENABLE if self.enable
                   else mpsse_cmd.LOOPBACK_DISABLE)

    def __repr__(self):
        return f"<Loopback {'on' if self.enable else 'off'}>"


class ClockDivisor(Operation):
    def __init__(self, divisor: int):
        self.divisor = divisor

    def encode(self, buf):
        d = self.divisor - 1
        buf.append(mpsse_cmd.CLK_DIV)
        buf.append(d & 0xff)
        buf.append(d >> 8)

    def __repr__(self):
        return f"<ClockDivisor {self.divisor}>"


class ClockDiv5(Operation):
    def __init__(self, enable: bool):
        self.enable = enable

    def encode(self, buf):
        buf.append(mpsse_cmd.CLK_DIV5_ENABLE if self.enable
                   else mpsse_cmd.CLK_DIV5_DISABLE)

    def __repr__(self):
        return f"<ClockDiv5 {'on' if self.enable else 'off'}>"


class ThreePhase(Operation):
    def __init__(self, enable: bool):
        self.enable = enable

    def encode(self, buf):
        buf.append(mpsse_cmd.THREE_PHASE_ENABLE if self.enable
                   else mpsse_cmd.THREE_PHASE_DISABLE)

    def __repr__(self):
        return f"<ThreePhase {'on' if self.enable else 'off'}>"


class Adaptive(Operation):
    def __init__(self, enable: bool):
        self.enable = enable

    def encode(self, buf):
        buf.append(mpsse_cmd.ADAPTIVE_ENABLE if self.enable
                   else mpsse_cmd.ADAPTIVE_DISABLE)

    def __repr__(self):
        return f"<Adaptive {'on' if self.enable else 'off'}>"


class SendImmediate(Operation):
    def encode(self, buf):
        buf.append(mpsse_cmd.SEND_IMMEDIATE)


class WaitOnHigh(Operation):
    def encode(self, buf):
        buf.append(mpsse_cmd.WAIT_ON_HIGH)


class WaitOnLow(Operation):
    def encode(self, buf):
        buf.append(mpsse_cmd.WAIT_ON_LOW)


class ClockBits(Operation):
    def __init__(self, count: int):
        assert 1 <= count <= 8
        self.count = count
        self.cycle_count = count

    def encode(self, buf):
        buf.append(mpsse_cmd.CLK_BITS)
        buf.append(self.count - 1)

    def __repr__(self):
        return f"<ClockBits {self.count}>"


class ClockBytes(Operation):
    def __init__(self, count: int):
        assert 1 <= count <= 65536
        self.count = count
        self.cycle_count = count

    def encode(self, buf):
        c = self.count - 1
        buf.append(mpsse_cmd.CLK_BYTES)
        buf.append(c & 0xff)
        buf.append(c >> 8)

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
        self.cycle_count = count
        self.rsp_size = 1 if read else 0

        cmd = mpsse_cmd.BITS
        if lsb_first:
            cmd |= mpsse_cmd.LSB
        if write_pol != "+":
            cmd |= mpsse_cmd.WRITE_NEG

        if data is not None:
            cmd |= mpsse_cmd.WRITE
            data = data or 0
            if not lsb_first:
                data <<= 8 - count
            self._data_byte = data & 0xff
            self._has_data = True
        else:
            self._data_byte = 0
            self._has_data = False

        if read:
            cmd |= mpsse_cmd.READ
            if read_pol != "+":
                cmd |= mpsse_cmd.READ_NEG

        self._cmd_byte = cmd

    def encode(self, buf):
        buf.append(self._cmd_byte)
        buf.append(self.count - 1)
        if self._has_data:
            buf.append(self._data_byte)

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

        self._cmd_byte = cmd
        self._data_out = data_out
        self.byte_count = byte_count
        self.read = read
        self.rsp_size = byte_count if read else 0
        self.cycle_count = byte_count * 8

    def encode(self, buf):
        c = self.byte_count - 1
        buf.append(self._cmd_byte)
        buf.append(c & 0xff)
        buf.append(c >> 8)
        if self._data_out:
            buf += self._data_out

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

        self._cmd_byte = cmd
        self._data_byte = (data & 0x7f) | ((tdi & 1) << 7)
        self.count = count
        self.write_pol = write_pol
        self.read_pol = read_pol
        self.read = read
        self.tms = BitString(data, count)
        self.tdi = int(bool(tdi))
        self.rsp_size = 1 if read else 0
        self.cycle_count = count

    def encode(self, buf):
        buf.append(self._cmd_byte)
        buf.append(self.count - 1)
        buf.append(self._data_byte)

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
        """Serialize the whole batch into one growing bytearray, then
        hand it to the transport. Each op writes its command bytes
        in place via ``encode(buf)`` — no per-op bytes allocation,
        no terminal ``b"".join``."""
        buf = bytearray()
        if self._bracket_pre:
            buf += self._bracket_pre

        total_rsp = 0
        for op, _future in batch:
            op.encode(buf)
            total_rsp += op.rsp_size

        if self._bracket_post:
            buf += self._bracket_post

        if total_rsp == 0:
            # Need at least 1 response byte to synchronize.
            buf.append(mpsse_cmd.GET_BITS_LOW)
            total_rsp = 1
        buf.append(mpsse_cmd.SEND_IMMEDIATE)

        if self.logger.isEnabledFor(PROTOCOL):
            self.logger.protocol(
                "USB >> %d bytes, expect %d back", len(buf), total_rsp)
        self._transport.write(bytes(buf))

        def read_done(rsp):
            try:
                data = rsp.result()
            except BaseException as exc:
                # USB failure: every batch future would otherwise dangle,
                # which deadlocks upper layers chained via add_done_callback.
                for _op, future in batch:
                    if not future.done():
                        future.set_exception(exc)
                return

            if self.logger.isEnabledFor(PROTOCOL):
                self.logger.protocol("USB << %d bytes", len(data))

            offset = 0
            for op, future in batch:
                rsp_size = op.rsp_size
                if rsp_size:
                    op.rsp_handle(data[offset:offset + rsp_size])
                    offset += rsp_size
                future.set_result(op)

        self._transport.read(total_rsp).add_done_callback(read_done)
