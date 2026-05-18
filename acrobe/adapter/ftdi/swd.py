"""SWD interface using FTDI MPSSE.

Translates :mod:`acrobe.protocol.swd` ops into MPSSE clock-data
commands plus an OE-pin toggle that drives an external bidirectional
buffer on the SWDIO line.

Hardware setup
--------------

MPSSE has TCK (bit 0), TDI (bit 1, output), TDO (bit 2, input) and
TMS (bit 3, output, unused here). SWD's SWDIO line is bidirectional,
so the adapter routes TDI and TDO through an external buffer whose
direction is controlled by an OE pin:

* ``oen_pin``  — active-low OE: pin LOW means *host drives SWDIO*.
* ``oe_pin``   — active-high OE: pin HIGH means *host drives SWDIO*.

Pass exactly one of the two; the polarity is purely a board choice
and the wire-level lowering doesn't care which it is.

Wire framing
------------

Per SWD packet we flip the OE pin twice — once between the host-driven
cmd byte and the target-driven ACK, once after the target window to
hand the line back. The TRN cycle between cmd and ACK is clocked with
OE already in target-drive mode (the line floats on its pull-up for
that one cycle); same trick on the way back.

The wire layer is deliberately raw: each ``swd.Read`` / ``swd.Write``
turns into exactly one wire packet, and the returned future resolves
with whatever the chip drove on that packet's data slot. The AP-read
posted-result is a Debug Port concern, not a wire concern — pipeline
bookkeeping lives in :class:`acrobe.component.arm.sw_dp.SwDp`'s
lowering, parallel to how :class:`acrobe.component.arm.jtag_dp.JtagDp`
handles JTAG-DP-side pipelining.
"""

from __future__ import annotations

import asyncio
import math

from .mpsse import (
    MpsseEngine, SetBitsLow, SetBitsHigh,
    ShiftBits, ShiftBytes, ClockBits, ClockBytes,
    ThreePhase, Adaptive, Loopback, ClockDiv5, ClockDivisor,
)
from ...bitstring import BitString
from ...protocol import swd


# --- SWD wire constants ---------------------------------------------

# 1 TRN cycle is the SWD default. The DLCR register can program up
# to 4; we don't yet expose that knob. If we ever need it, plumb a
# ``turnaround_cycles`` setter through to flush_ops.
_TRN_CYCLES = 1

_RDBUFF_REG = 0x0c

# TARGETSEL is at the same byte offset as RDBUFF (the address space
# overloads the W direction); the spec disambiguates by RnW.
_TARGETSEL_REG = 0x0c

_ACK_OK    = swd.Ack.OK
_ACK_WAIT  = swd.Ack.WAIT
_ACK_FAULT = swd.Ack.FAULT


def _swd_cmd_byte(ap: bool, rnw: bool, addr: int) -> int:
    """8-bit SWD request packet (LSB-first wire order):

        bit 0: start (1)
        bit 1: APnDP
        bit 2: RnW
        bit 3: A[2]
        bit 4: A[3]
        bit 5: parity (even over bits 1..4)
        bit 6: stop (0)
        bit 7: park (1)
    """
    a = (addr >> 2) & 0x3
    parity = (int(ap) ^ (a & 1) ^ ((a >> 1) & 1) ^ int(rnw)) & 1
    return (0x81 | (0x4 if rnw else 0)
            | (int(ap) << 1)
            | (a << 3)
            | (parity << 5))


def _data_parity(data: int) -> int:
    x = data & 0xFFFFFFFF
    x ^= x >> 16
    x ^= x >> 8
    x ^= x >> 4
    x ^= x >> 2
    x ^= x >> 1
    return x & 1


# --- The SWD interface ----------------------------------------------


class SwdMpsse(swd.Interface):
    """SWD interface lowered to FTDI MPSSE bit-bang."""

    SIDE_HOST   = "host"
    SIDE_TARGET = "target"

    def __init__(self, engine: MpsseEngine, *,
                 oen_pin: int | None = None,
                 oe_pin: int | None = None,
                 name: str = "swd"):
        super().__init__(name=name)
        if (oen_pin is None) == (oe_pin is None):
            raise ValueError(
                "SwdMpsse: pass exactly one of oen_pin / oe_pin")
        self.__engine = engine
        # Pin index 0..15. Bits 0..3 are TCK/TDI/TDO/TMS; the OE pin
        # MUST land in 4..15 to avoid clobbering MPSSE's own signals.
        bit = oe_pin if oen_pin is None else oen_pin
        if not (4 <= bit <= 15):
            raise ValueError(
                f"SwdMpsse: OE pin must be in [4..15], got {bit}")
        self.__oe_bit = bit
        # Value driven on the OE pin to make the host source SWDIO:
        # active-low OE → 0; active-high OE → 1.
        self.__oe_host_value = 0 if oen_pin is not None else 1
        self.__gpio_oe = 0
        self.__gpio_val = 0
        self.__side = None       # current OE side; flushed lazily.
        self.__clock_state = None
        self.__read_pol = "+"

    # --- frequency / clock-divisor (mirrors JtagMpsse) ---------------

    def freq_update(self, freq):
        if freq is None:
            self.__read_pol = "+"
            return None

        best = None
        for try_div5, base in [(False, 60e6), (True, 12e6)]:
            divisor = max(1, math.ceil(base / (2 * freq)))
            actual = base / (2 * divisor)
            if actual <= freq and (best is None or actual > best[0]):
                best = (actual, try_div5, divisor)
        actual, div5, divisor = best

        # Sample TDO on the falling edge above 10 MHz for setup margin.
        self.__read_pol = "-" if actual >= 10e6 else "+"

        new_state = (div5, divisor)
        if new_state != self.__clock_state:
            self.__clock_state = new_state
            self.__engine.post(ClockDiv5(div5))
            self.__engine.post(ClockDivisor(divisor))

        return actual

    # --- setup ------------------------------------------------------

    async def setup(self, gpio_oe: int = 0, gpio_val: int = 0,
                    freq: float = 1e6):
        """Initialise MPSSE for SWD: TCK/TDI as outputs, TDO as input,
        TMS as output (idle low; unused for SWD). The OE pin starts
        with the host driving SWDIO so the first transaction can post
        a cmd byte without an extra preamble flip.

        ``gpio_oe`` / ``gpio_val`` are the board-level GPIO bits the
        adapter wrapper passes in (e.g. buffer enables, LEDs); we OR
        in TCK/TDI/TMS/OE on top.
        """
        # TCK=bit0, TDI=bit1, TMS=bit3 are outputs; TDO=bit2 is input.
        mpsse_oe = 0x0B | (1 << self.__oe_bit)
        self.__gpio_oe = mpsse_oe | gpio_oe
        # Force the OE pin to the host-drive value at idle.
        bit_mask = 1 << self.__oe_bit
        oe_val = self.__oe_host_value << self.__oe_bit
        self.__gpio_val = (gpio_val & ~bit_mask) | oe_val
        self.__side = self.SIDE_HOST

        self.freq_update(freq)

        ops = [
            ThreePhase(False),
            Adaptive(False),
            Loopback(False),
            SetBitsLow(self.__gpio_val & 0xFF, self.__gpio_oe & 0xFF),
        ]
        if self.__gpio_oe & 0xFF00:
            ops.append(SetBitsHigh(
                (self.__gpio_val >> 8) & 0xFF,
                (self.__gpio_oe >> 8) & 0xFF))

        futures = [self.__engine.post(op) for op in ops]
        await asyncio.gather(*futures)

    # --- OE flipping ------------------------------------------------

    def __set_side(self, mpsse_ops, side):
        """Flip the OE pin so the *side* drives SWDIO. Cheap no-op
        when we're already on that side."""
        if self.__side == side:
            return
        target_value = (self.__oe_host_value if side == self.SIDE_HOST
                        else 1 - self.__oe_host_value)
        bit_mask = 1 << self.__oe_bit
        if target_value:
            self.__gpio_val |= bit_mask
        else:
            self.__gpio_val &= ~bit_mask
        if self.__oe_bit < 8:
            mpsse_ops.append(SetBitsLow(
                self.__gpio_val & 0xFF, self.__gpio_oe & 0xFF))
        else:
            mpsse_ops.append(SetBitsHigh(
                (self.__gpio_val >> 8) & 0xFF,
                (self.__gpio_oe >> 8) & 0xFF))
        self.__side = side

    # --- Wire helpers ------------------------------------------------

    def __shift_run(self, mpsse_ops, cycles, value):
        """Clock ``cycles`` cycles with TDI driven to ``value``. Caller
        owns the OE side; this is just bit-shifting."""
        if cycles <= 0:
            return
        # Up to 8 trailing bits via ShiftBits (which still drives TDI);
        # a leading bulk via ShiftBytes for any 8-bit-aligned head.
        # For all-zero / all-one streams we could use ClockBits/Bytes
        # to avoid sending the data byte — at the cost of leaving TDI
        # at whatever its last state was. Be explicit: drive TDI to
        # the requested value the whole way through.
        byte_value = 0xFF if value else 0x00
        full_bytes, tail = divmod(cycles, 8)
        if full_bytes:
            blob = bytes([byte_value]) * full_bytes
            for off in range(0, len(blob), 65536):
                mpsse_ops.append(ShiftBytes(blob[off:off + 65536]))
        if tail:
            mpsse_ops.append(ShiftBits(byte_value, tail))

    def __shift_word(self, mpsse_ops, value, bits):
        """Drive a small (≤32-bit) value LSB-first."""
        full_bytes, tail = divmod(bits, 8)
        if full_bytes:
            blob = bytes((value >> (8 * i)) & 0xFF
                         for i in range(full_bytes))
            mpsse_ops.append(ShiftBytes(blob))
        if tail:
            mpsse_ops.append(ShiftBits(
                (value >> (8 * full_bytes)) & ((1 << tail) - 1), tail))

    # --- Per-op lowering --------------------------------------------

    def __emit_read(self, mpsse_ops, ap, addr):
        """Returns (ack_op, data_op, parity_op) — MPSSE Operation
        instances whose ``data`` fields hold the captured ACK/DATA/
        parity bits after the batch executes."""
        cmd = _swd_cmd_byte(ap, True, addr)
        # Host drives the cmd byte.
        self.__set_side(mpsse_ops, self.SIDE_HOST)
        mpsse_ops.append(ShiftBits(cmd, 8))
        # TRN + ACK + DATA + parity all happen with target driving.
        self.__set_side(mpsse_ops, self.SIDE_TARGET)
        # 1 TRN cycle (clocked but ignored on receive).
        mpsse_ops.append(ShiftBits(0, _TRN_CYCLES, read=True))
        ack_op = ShiftBits(None, 3, read=True, read_pol=self.__read_pol)
        mpsse_ops.append(ack_op)
        data_op = ShiftBytes(4, read=True, read_pol=self.__read_pol)
        mpsse_ops.append(data_op)
        par_op = ShiftBits(None, 1, read=True, read_pol=self.__read_pol)
        mpsse_ops.append(par_op)
        # Hand the line back — TRN with host now driving (line still
        # in transition; pull-up keeps it sane).
        self.__set_side(mpsse_ops, self.SIDE_HOST)
        mpsse_ops.append(ShiftBits(0, _TRN_CYCLES))
        return ack_op, data_op, par_op

    def __emit_write(self, mpsse_ops, ap, addr, data):
        cmd = _swd_cmd_byte(ap, False, addr)
        self.__set_side(mpsse_ops, self.SIDE_HOST)
        mpsse_ops.append(ShiftBits(cmd, 8))
        self.__set_side(mpsse_ops, self.SIDE_TARGET)
        mpsse_ops.append(ShiftBits(0, _TRN_CYCLES, read=True))
        ack_op = ShiftBits(None, 3, read=True, read_pol=self.__read_pol)
        mpsse_ops.append(ack_op)
        # Host takes the line back to drive the data + parity.
        self.__set_side(mpsse_ops, self.SIDE_HOST)
        mpsse_ops.append(ShiftBits(0, _TRN_CYCLES))
        data &= 0xFFFFFFFF
        mpsse_ops.append(ShiftBytes(data.to_bytes(4, "little")))
        mpsse_ops.append(ShiftBits(_data_parity(data), 1))
        return ack_op

    def __emit_targetsel_write(self, mpsse_ops, target: int):
        """Wire-level TARGETSEL write. Per ADIv5/v6 spec, no DP
        responds with an ACK on this transaction — the addressed DP
        latches the value silently and the others go untouched. The
        bit framing mirrors a normal write, but we don't capture the
        ACK bits (any value is acceptable, including the all-high
        "no driver / pull-up" state)."""
        cmd = _swd_cmd_byte(False, False, _TARGETSEL_REG)
        self.__set_side(mpsse_ops, self.SIDE_HOST)
        mpsse_ops.append(ShiftBits(cmd, 8))
        # Hand the line to the target for the TRN+ACK window; we
        # discard whatever appears there.
        self.__set_side(mpsse_ops, self.SIDE_TARGET)
        mpsse_ops.append(ShiftBits(0, _TRN_CYCLES, read=True))
        mpsse_ops.append(ShiftBits(0, 3, read=True))
        # Host drives the second TRN, data, and parity.
        self.__set_side(mpsse_ops, self.SIDE_HOST)
        mpsse_ops.append(ShiftBits(0, _TRN_CYCLES))
        data = target & 0xFFFFFFFF
        mpsse_ops.append(ShiftBytes(data.to_bytes(4, "little")))
        mpsse_ops.append(ShiftBits(_data_parity(data), 1))

    # --- Batch execution --------------------------------------------

    async def flush_wire_ops(self, batch):
        mpsse_ops = []
        # Per-record: [user_future, kind, ack_op, data_op_or_None,
        #              parity_op_or_None]. Each record covers exactly
        # one wire packet — no pipeline awareness.
        records: list[list] = []

        for op, future in batch:
            if isinstance(op, swd.Run):
                self.__set_side(mpsse_ops, self.SIDE_HOST)
                self.__shift_run(mpsse_ops, op.cycles, 0)
                future.set_result(None)
                continue

            if isinstance(op, swd.Wakeup):
                self.__set_side(mpsse_ops, self.SIDE_HOST)
                self.__shift_run(mpsse_ops, op.cycles, 1)
                future.set_result(None)
                continue

            if isinstance(op, swd.LineReset):
                self.__set_side(mpsse_ops, self.SIDE_HOST)
                self.__shift_run(mpsse_ops, 60, 1)
                self.__shift_run(mpsse_ops, 8, 0)
                future.set_result(None)
                continue

            if isinstance(op, swd.JtagToSwd):
                self.__set_side(mpsse_ops, self.SIDE_HOST)
                # Standard wakeup pattern: line reset + 0xE79E switch
                # (LSB-first) + line reset + idle. Idempotent.
                self.__shift_run(mpsse_ops, 60, 1)
                self.__shift_word(mpsse_ops, 0xE79E, 16)
                self.__shift_run(mpsse_ops, 60, 1)
                self.__shift_word(mpsse_ops, 0xE79E, 16)
                self.__shift_run(mpsse_ops, 60, 1)
                self.__shift_run(mpsse_ops, 8, 0)
                future.set_result(None)
                continue

            if isinstance(op, swd.SwdToDormant):
                self.__set_side(mpsse_ops, self.SIDE_HOST)
                # ≥50 cycles SWDIO=1 then the 16-bit 0xE3BC pattern
                # (LSB-first on the wire matches the spec's wire-order
                # listing of the bit stream).
                self.__shift_run(mpsse_ops, 60, 1)
                self.__shift_word(mpsse_ops, 0xE3BC, 16)
                future.set_result(None)
                continue

            if isinstance(op, swd.DormantToSwd):
                self.__set_side(mpsse_ops, self.SIDE_HOST)
                # 8 cycles SWDIO=1 + 128-bit selection alert +
                # 4 cycles of 0 + 8-bit SWD activation 0x1A.
                # ARM IHI0031F section B5.3.2 / Table B5-3.
                self.__shift_run(mpsse_ops, 8, 1)
                # Selection alert sequence, 128 bits, defined as
                # 0x49CF9046A9B4A161 0x97F5BBC719B40F38 when read
                # MSB-first; on the LSB-first wire we shift the
                # bit-reversed words.
                self.__shift_word(mpsse_ops, 0x86852D956209F392, 64)
                self.__shift_word(mpsse_ops, 0x19BC0EA2E3DDAFE9, 64)
                self.__shift_run(mpsse_ops, 4, 0)
                self.__shift_word(mpsse_ops, 0x1A, 8)
                future.set_result(None)
                continue

            if isinstance(op, swd.TargetSelWrite):
                self.__emit_targetsel_write(mpsse_ops, op.target)
                future.set_result(None)
                continue

            if isinstance(op, swd.Read):
                # One packet, one future resolving with the chip-
                # driven data field of THIS packet. The caller is
                # responsible for interpreting AP-read pipelining
                # semantics (see SwDp.flush_ops).
                ack_op, data_op, par_op = self.__emit_read(
                    mpsse_ops, op.ap, op.addr)
                kind = "ap_read" if op.ap else "dp_read"
                records.append([future, kind, ack_op, data_op, par_op])
                continue

            if isinstance(op, swd.Write):
                ack_op = self.__emit_write(
                    mpsse_ops, op.ap, op.addr, op.data)
                kind = "ap_write" if op.ap else "dp_write"
                records.append([future, kind, ack_op, None, None])
                continue

            future.set_exception(TypeError(
                f"SwdMpsse can't lower {type(op).__name__}"))

        if not mpsse_ops:
            return

        try:
            futures = [self.__engine.post(op) for op in mpsse_ops]
            await asyncio.gather(*futures)
        except Exception as exc:
            for rec in records:
                if rec[0] is not None and not rec[0].done():
                    rec[0].set_exception(exc)
            raise

        for fut, kind, ack_op, data_op, par_op in records:
            ack_bits = ack_op.data
            ack = (int(ack_bits[0])
                   | (int(ack_bits[1]) << 1)
                   | (int(ack_bits[2]) << 2))
            if fut is None:
                if ack != _ACK_OK:
                    self.logger.warning(
                        "SWD %s ACK=0b%s", kind, format(ack, "03b"))
                continue
            if fut.done():
                continue
            if ack == _ACK_OK:
                if data_op is None:
                    fut.set_result(None)
                else:
                    blob = data_op.data
                    val = (blob[0] | (blob[1] << 8)
                           | (blob[2] << 16) | (blob[3] << 24))
                    par = int(par_op.data[0])
                    if par != _data_parity(val):
                        fut.set_exception(swd.SwdAccessFailure(
                            f"parity error on {kind} (data=0x{val:08x})"))
                    else:
                        fut.set_result(val)
            elif ack == _ACK_WAIT:
                fut.set_exception(swd.SwdWait(
                    f"WAIT on {kind} (retry not implemented)"))
            elif ack == _ACK_FAULT:
                fut.set_exception(swd.SwdAccessFailure(f"FAULT on {kind}"))
            else:
                fut.set_exception(swd.SwdAccessFailure(
                    f"invalid ACK 0b{ack:03b} on {kind}"))
