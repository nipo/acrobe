from __future__ import annotations

import asyncio
import math

from .mpsse import (
    MpsseEngine, GetBitsLow, SetBitsLow, ShiftBytes,
    ThreePhase, Adaptive, Loopback, ClockDiv5, ClockDivisor,
)
from ...protocol import spi


class SpiMpsse(spi.Interface):
    """SPI master using FTDI MPSSE.

    Translates SPI protocol operations (Cs, Shift) into MPSSE
    operations and posts them to an `MpsseEngine`.

    MPSSE low byte pin map:
      bit 0: SCK (output)
      bit 1: MOSI (output)
      bit 2: MISO (input)
      bit 3: unused (TMS)
      bits 4-7: GPIO

    Chip select is one of the GPIO bits, driven active-low. Further
    GPIO bits are declared through `gpio_oe` / `gpio_val` and driven
    through `gpio_set` / `gpio_oe_set`; inputs are sampled with
    `gpio_get`. Board layers use those to hold reset lines and to
    read status pins that share the byte.

    All four SPI modes are supported: `Cs.mode` selects the clock
    idle level and the shift polarities for the transaction that
    follows.
    """

    SCK = 0
    MOSI = 1
    MISO = 2

    # One MPSSE shift carries at most 65536 bytes.
    MAX_SHIFT = 65536

    # Clock applied when nothing else constrains the interface.
    DEFAULT_FREQ = 1e6

    def __init__(self, engine: MpsseEngine, *, cs_pin: int,
                 gpio_oe: int = 0, gpio_val: int = 0, name: str = "spi"):
        spi.Interface.__init__(self, None, name)
        self.__engine = engine
        self.__cs_mask = 1 << cs_pin
        self.__oe = ((1 << self.SCK) | (1 << self.MOSI)
                     | self.__cs_mask | gpio_oe)
        # Chip select idles deasserted.
        self.__val = gpio_val | self.__cs_mask
        self.__clock_state = None
        self.__write_pol = "-"
        self.__read_pol = "+"
        self.child_add(spi.Target(self, cs=0, mode=0, name="cs0"))

    @property
    def gpio_value(self) -> int:
        return self.__val

    @property
    def gpio_oe(self) -> int:
        return self.__oe

    async def start(self):
        """Put the channel in a known MPSSE state and drive the
        declared output levels."""
        if self.freq is None:
            self.freq_update(self.DEFAULT_FREQ)
        futures = [self.__engine.post(op) for op in (
            ThreePhase(False),
            Adaptive(False),
            Loopback(False),
            SetBitsLow(self.__val & 0xFF, self.__oe & 0xFF),
        )]
        await asyncio.gather(*futures)

    def freq_update(self, freq):
        """Compute and apply the clock divisor for `freq`.

        Returns the actual achieved frequency (never exceeds `freq`).
        Posts ClockDiv5 + ClockDivisor only when the divisor changed.
        """
        if freq is None:
            return None

        best = None
        for try_div5, base in ((False, 60e6), (True, 12e6)):
            divisor = max(1, math.ceil(base / (2 * freq)))
            actual = base / (2 * divisor)
            if actual <= freq and (best is None or actual > best[0]):
                best = (actual, try_div5, divisor)
        actual, div5, divisor = best

        state = (div5, divisor)
        if state != self.__clock_state:
            self.__clock_state = state
            self.__engine.post(ClockDiv5(div5))
            self.__engine.post(ClockDivisor(divisor))
        return actual

    # --- GPIO access for board layers ---

    async def gpio_set(self, mask: int, value: int):
        """Drive the pins selected by `mask` to the levels in `value`."""
        self.__val = (self.__val & ~mask) | (value & mask)
        await self.__engine.post(
            SetBitsLow(self.__val & 0xFF, self.__oe & 0xFF))

    async def gpio_oe_set(self, mask: int, oe: int):
        """Set the direction of the pins selected by `mask`
        (1 = output) without changing their levels."""
        self.__oe = (self.__oe & ~mask) | (oe & mask)
        await self.__engine.post(
            SetBitsLow(self.__val & 0xFF, self.__oe & 0xFF))

    async def gpio_get(self) -> int:
        """Sample the whole low byte, inputs included."""
        op = await self.__engine.post(GetBitsLow())
        return op.value

    # --- Op lowering ---

    async def flush_ops(self, batch):
        mpsse_ops = []
        # Per batch op: (op, future, miso_range_or_None).
        entries = []

        for op, future in batch:
            if isinstance(op, spi.Cs):
                self.__emit_cs(mpsse_ops, op)
                entries.append((op, future, None))
            elif isinstance(op, spi.Shift):
                entries.append((op, future, self.__emit_shift(mpsse_ops, op)))
            elif future is not None and not future.done():
                future.set_exception(TypeError(
                    f"SpiMpsse cannot lower {type(op).__name__}"))

        if not mpsse_ops:
            self.__resolve(entries, mpsse_ops)
            return

        for op in mpsse_ops[:-1]:
            self.__engine.post_no_wait(op)
        anchor = self.__engine.post(mpsse_ops[-1])
        anchor.add_done_callback(
            lambda f, e=entries, m=mpsse_ops: SpiMpsse.__resolve_batch(e, m, f))

    @staticmethod
    def __resolve_batch(entries, mpsse_ops, anchor):
        try:
            anchor.result()
        except Exception as exc:
            for _op, future, _span in entries:
                if future is not None and not future.done():
                    future.set_exception(exc)
            return
        SpiMpsse.__resolve(entries, mpsse_ops)

    @staticmethod
    def __resolve(entries, mpsse_ops):
        """Populate `Shift.miso` from the MPSSE ops each shift was
        lowered to, and resolve every batch future. A reading shift
        resolves with its miso bytes, anything else with None."""
        for op, future, span in entries:
            miso = None
            if span is not None:
                start, end = span
                miso = b"".join(m.data for m in mpsse_ops[start:end])
            if isinstance(op, spi.Shift):
                op.miso = miso
            if future is not None and not future.done():
                future.set_result(miso)

    def __emit_cs(self, mpsse_ops, op):
        """Assert or release chip select, applying the transaction's
        SPI mode. Clock idle level is moved before the select edge so
        the target never sees a clock transition while selected."""
        if op.value is None:
            self.__val |= self.__cs_mask
            mpsse_ops.append(SetBitsLow(self.__val & 0xFF, self.__oe & 0xFF))
            return

        cpol = (op.mode >> 1) & 1
        cpha = op.mode & 1
        self.__write_pol = "+" if cpol != cpha else "-"
        self.__read_pol = "-" if cpol != cpha else "+"

        sck = 1 << self.SCK
        idle = (self.__val & ~sck) | (sck if cpol else 0)
        if idle != self.__val:
            self.__val = idle
            mpsse_ops.append(SetBitsLow(self.__val & 0xFF, self.__oe & 0xFF))

        self.__val &= ~self.__cs_mask
        mpsse_ops.append(SetBitsLow(self.__val & 0xFF, self.__oe & 0xFF))

    def __emit_shift(self, mpsse_ops, op):
        """Emit the MPSSE shifts for one SPI shift. Returns the
        (start, end) range of ops carrying MISO, or None."""
        data = op.mosi if isinstance(op.mosi, (bytes, bytearray)) \
            else bytes(op.mosi)
        if not data:
            return None
        start = len(mpsse_ops)
        for offset in range(0, len(data), self.MAX_SHIFT):
            mpsse_ops.append(ShiftBytes(
                bytes(data[offset:offset + self.MAX_SHIFT]),
                write_pol=self.__write_pol,
                read_pol=self.__read_pol,
                lsb_first=False,
                read=op.read_miso))
        if not op.read_miso:
            return None
        return (start, len(mpsse_ops))
