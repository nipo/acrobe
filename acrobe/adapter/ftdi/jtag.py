from __future__ import annotations

import asyncio
import math

from .mpsse import (
    MpsseEngine, SetBitsLow, SetBitsHigh,
    ShiftBits, ShiftBytes, ShiftTms, ClockBits, ClockBytes,
    ThreePhase, Adaptive, Loopback, ClockDiv5, ClockDivisor,
)
from ...bitstring import BitString, BitStringBase
from ...protocol import jtag


class JtagMpsse(jtag.JtagInterface):
    """JTAG interface using FTDI MPSSE.

    Translates JTAG protocol operations (CaptureDr, CaptureIr, Shift,
    Run, Reset, SwdToJtag) into MPSSE operations and posts them to
    an MpsseEngine. Maintains the JTAG TAP state machine.

    MPSSE low byte pin map:
      bit 0: TCK (output)
      bit 1: TDI (output)
      bit 2: TDO (input)
      bit 3: TMS (output)
      bits 4-7: GPIO L0-L3
    """

    STATE_UNKNOWN = "unknown"
    STATE_RESET = "reset"
    STATE_RTI = "rti"
    STATE_PAUSE = "pause"

    def __init__(self, engine: MpsseEngine):
        super().__init__()
        self._engine = engine
        self._state = self.STATE_UNKNOWN
        self._gpio_oe = 0
        self._gpio_val = 0
        self._clock_state = None
        self._read_pol = "+"

    def freq_update(self, freq):
        """Compute and apply clock divisor for the requested frequency.

        Returns the actual achieved frequency (never exceeds freq).
        Posts ClockDiv5 + ClockDivisor ops only if the divisor changed.

        At >= 10 MHz, switches TDO sampling to falling edge for better
        setup margin (matches crobe behavior).
        """
        if freq is None:
            self._read_pol = "+"
            return None

        # Clock calculation for FT2232H (60 MHz base):
        # With DIV5 enabled: base = 12 MHz, freq = 12e6 / (divisor * 2)
        # With DIV5 disabled: base = 60 MHz, freq = 60e6 / (divisor * 2)
        #
        # Try both modes and pick the highest actual freq that
        # does not exceed the requested cap.
        best = None
        for try_div5, base in [(False, 60e6), (True, 12e6)]:
            divisor = max(1, math.ceil(base / (2 * freq)))
            actual = base / (2 * divisor)
            if actual <= freq and (best is None or actual > best[0]):
                best = (actual, try_div5, divisor)
        actual, div5, divisor = best

        # At high clock rates, sample TDO on the falling edge to give
        # extra propagation margin (full period instead of half).
        self._read_pol = "-" if actual >= 10e6 else "+"

        new_state = (div5, divisor)
        if new_state != self._clock_state:
            self._clock_state = new_state
            self._engine.post(ClockDiv5(div5))
            self._engine.post(ClockDivisor(divisor))

        return actual

    async def setup(self, gpio_oe=0, gpio_val=0, freq=1e6):
        """Initialize MPSSE for JTAG and configure GPIO pins.

        Sets TCK, TDI, TMS as outputs (low); TDO as input.
        Additional GPIO bits can be configured via gpio_oe/gpio_val
        (bits 4-15).

        Sends FTDI H-series MPSSE init commands (3-phase disable,
        adaptive disable, loopback disable) and sets the clock to freq Hz.
        Default 1 MHz matches the old sync code's initial clock.
        """
        # TCK=bit0, TDI=bit1, TMS=bit3 as outputs; TDO=bit2 as input
        jtag_oe = 0x0B
        self._gpio_oe = jtag_oe | gpio_oe
        self._gpio_val = gpio_val

        self.freq_update(freq)

        ops = [
            ThreePhase(False),
            Adaptive(False),
            Loopback(False),
            SetBitsLow(self._gpio_val & 0xFF, self._gpio_oe & 0xFF),
        ]
        if self._gpio_oe & 0xFF00:
            ops.append(SetBitsHigh(
                (self._gpio_val >> 8) & 0xFF,
                (self._gpio_oe >> 8) & 0xFF))

        futures = [self._engine.post(op) for op in ops]
        await asyncio.gather(*futures)
        self._state = self.STATE_UNKNOWN

    async def flush_ops(self, batch):
        """Translate JTAG operations to MPSSE and execute."""
        self.logger.log(5, "JTAG batch: %s", [op for op, _ in batch])
        mpsse_ops = []
        # Track Shift ops that need TDO extraction:
        # (batch_index, mpsse_start_index, mpsse_end_index)
        tdo_entries = []

        for idx, (op, future) in enumerate(batch):
            if isinstance(op, (jtag.Reset, jtag.SwdToJtag)):
                self._emit_tms_pattern(mpsse_ops, op.tms)
                self._state = self.STATE_RESET

            elif isinstance(op, jtag.Run):
                self._emit_run(mpsse_ops, op.cycles)

            elif isinstance(op, jtag.CaptureDr):
                self._emit_capture_dr(mpsse_ops)

            elif isinstance(op, jtag.CaptureIr):
                self._emit_capture_ir(mpsse_ops)

            elif isinstance(op, jtag.Shift):
                start, end = self._emit_shift(mpsse_ops, op)
                if op.read_tdo:
                    tdo_entries.append((idx, start, end))

            else:
                raise ValueError(f"Unknown JTAG op: {type(op).__name__}")

        # Post all MPSSE ops to engine and await
        if mpsse_ops:
            futures = [self._engine.post(op) for op in mpsse_ops]
            await asyncio.gather(*futures)

        # Reconstruct TDO from MPSSE results
        captured: dict[int, BitString] = {}
        for batch_idx, mpsse_start, mpsse_end in tdo_entries:
            tdo = BitString()
            for m_op in mpsse_ops[mpsse_start:mpsse_end]:
                if hasattr(m_op, 'data') and m_op.data is not None:
                    tdo += m_op.data
            captured[batch_idx] = tdo

        # Resolve futures with the captured TDO (or None for ops with
        # no read).
        for idx, (_, future) in enumerate(batch):
            future.set_result(captured.get(idx))

    # --- State machine helpers ---

    def _emit_tms_pattern(self, mpsse_ops, tms):
        """Emit TMS bit pattern as ShiftTms ops (max 7 bits each)."""
        for off in range(0, len(tms), 7):
            chunk = min(7, len(tms) - off)
            mpsse_ops.append(ShiftTms(int(tms[off:off + chunk]), chunk))

    def _emit_run(self, mpsse_ops, cycles):
        """Emit Run-Test/Idle transition and clock cycles."""
        if self._state == self.STATE_PAUSE:
            # Pause → Exit2 → Update
            mpsse_ops.append(ShiftTms(0b11, 2))
            self._state = self.STATE_RTI
        elif self._state == self.STATE_RESET:
            self._state = self.STATE_RTI

        if self._state == self.STATE_RTI:
            # TMS=0 keeps us in RTI (or enters RTI from TLR/Update)
            mpsse_ops.append(ShiftTms(0b0, 1))
            left = max(cycles - 1, 0)
            while left >= 8:
                c = min(left, 65536 * 8) & ~7
                mpsse_ops.append(ClockBytes(c // 8))
                left -= c
            if left:
                mpsse_ops.append(ClockBits(left))
        else:
            # Unknown state: reset first
            mpsse_ops.append(ShiftTms(0b11111, 5))
            mpsse_ops.append(ShiftTms(0b0, 1))
            self._state = self.STATE_RTI

    def _emit_capture_dr(self, mpsse_ops):
        """Emit RTI/Update → Capture-DR → Pause-DR transition."""
        if self._state == self.STATE_PAUSE:
            # Pause → Exit2 → Update (then fall through)
            mpsse_ops.append(ShiftTms(0b11, 2))
        elif self._state != self.STATE_RTI:
            raise ValueError(f"CaptureDr from unexpected state: {self._state}")
        # RTI/Update → Select-DR → Capture-DR → Exit1-DR → Pause-DR
        mpsse_ops.append(ShiftTms(0b0101, 4))
        self._state = self.STATE_PAUSE

    def _emit_capture_ir(self, mpsse_ops):
        """Emit RTI/Update → Capture-IR → Pause-IR transition."""
        if self._state == self.STATE_PAUSE:
            # Pause → Exit2 → Update (then fall through)
            mpsse_ops.append(ShiftTms(0b11, 2))
        elif self._state != self.STATE_RTI:
            raise ValueError(f"CaptureIr from unexpected state: {self._state}")
        # RTI/Update → Sel-DR → Sel-IR → Cap-IR → Ex1-IR → Pause-IR
        mpsse_ops.append(ShiftTms(0b01011, 5))
        self._state = self.STATE_PAUSE

    def _emit_shift(self, mpsse_ops, op):
        """Emit Pause → Shift → data → Exit1 → Pause.

        Returns (mpsse_start, mpsse_end) indices of ops contributing to TDO.
        """
        assert self._state == self.STATE_PAUSE

        tdi = op.tdi
        read = op.read_tdo

        if not isinstance(tdi, BitStringBase):
            raise TypeError(f"Expected BitString for tdi, got {type(tdi)}")

        cycle_count = len(tdi)
        if cycle_count == 0:
            return (len(mpsse_ops), len(mpsse_ops))

        # Pause → Exit2 → Shift
        mpsse_ops.append(ShiftTms(0b01, 2))

        mpsse_start = len(mpsse_ops)

        left = cycle_count - 1
        offset = 0

        # Shift all but the last bit
        read_pol = self._read_pol
        while left >= 8:
            c = min(left, 65536 * 8) & ~7
            chunk = tdi[offset:offset + c]
            mpsse_ops.append(ShiftBytes(chunk, read=read, read_pol=read_pol))
            offset += c
            left -= c

        if left > 0:
            chunk = int(tdi[offset:offset + left])
            mpsse_ops.append(ShiftBits(chunk, left, read=read, read_pol=read_pol))
            offset += left

        # Last bit via TMS: shifts the bit AND transitions to Exit1
        last_bit = int(tdi[-1])
        mpsse_ops.append(ShiftTms(0b1, 1, tdi=last_bit, read=read, read_pol=read_pol))

        mpsse_end = len(mpsse_ops)

        # Exit1 → Pause
        mpsse_ops.append(ShiftTms(0b0, 1))

        # State remains PAUSE
        return (mpsse_start, mpsse_end)
