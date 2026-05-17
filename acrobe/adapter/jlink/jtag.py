"""J-Link JTAG bit-bang via CMD_JTAG_IO_V3.

Subclass of :class:`JtagInterface` whose ``flush_ops`` accumulates
the bit-level ops in a batch into a single TMS+TDI byte stream,
issues one ``jtag_io`` USB transaction, and demultiplexes the TDO
response into per-Shift futures.

Maintains the JTAG TAP state machine between ops with the same
PAUSE-DR/IR convention the FTDI driver uses (``CaptureDr/Ir`` ends
in PAUSE-DR/IR; ``Shift`` starts there, ends there). This keeps
back-to-back shifts cheap and matches how Chain.flush_ops feeds
TapOps."""

from __future__ import annotations

from ...bitstring import BitString
from ...protocol import jtag
from . import protocol
from .transport import JLinkTransport


def _pack_bits(bits) -> bytes:
    """Pack an iterable of 0/1 ints into LSB-first bytes."""
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _unpack_bits(data: bytes, count: int):
    """Unpack ``count`` bits from a LSB-first byte stream."""
    return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]


class JtagJlink(jtag.JtagInterface):
    """JtagInterface implementation backed by J-Link's bit-bang
    JTAG_IO_V3 command. One USB transaction per :meth:`flush_ops`
    batch — the underlying batcher already coalesces enough
    bit-level work to amortise the round-trip cost.

    State machine: same shape as the FTDI MPSSE driver (UNKNOWN ->
    RESET -> RTI -> PAUSE-DR/IR), so the existing Chain ↔ Tap
    pipeline drives this transport unchanged."""

    STATE_UNKNOWN = "unknown"
    STATE_RESET   = "reset"      # TLR (Test-Logic-Reset)
    STATE_RTI     = "rti"        # Run-Test/Idle
    STATE_PAUSE   = "pause"      # Pause-DR or Pause-IR

    def __init__(self, transport: JLinkTransport, name: str = "jtag"):
        super().__init__(name=name)
        self.__transport = transport
        self.__state = self.STATE_UNKNOWN

    async def setup(self, freq_khz: int = 1000):
        """Switch the J-Link to JTAG mode, release the target's
        reset lines, set the clock speed.

        Without the reset deassertion, the target stays held in
        reset and TDO floats — chain discovery sees "TDO stuck
        high"."""
        await self.__transport.select_interface(protocol.TIF_JTAG)
        await self.__transport.deassert_reset()
        await self.__transport.set_speed_khz(freq_khz)
        self.__state = self.STATE_UNKNOWN

    async def flush_ops(self, batch):
        tms: list[int] = []
        tdi: list[int] = []
        # Per-Shift records: (future, bit_start, bit_end, length).
        # bit_start/end pick out the TDI bits whose TDO replies form
        # the result; length is the original tdi width so we can
        # rebuild a same-sized BitString.
        shift_jobs: list[tuple] = []
        plain: list = []

        for op, future in batch:
            if isinstance(op, (jtag.Reset, jtag.SwdToJtag)):
                self.__emit_pattern(tms, tdi, op.tms)
                self.__state = self.STATE_RESET
                plain.append(future)
            elif isinstance(op, jtag.Run):
                self.__goto_rti(tms, tdi)
                # Run cycles in RTI = TMS=0 each cycle.
                tms.extend([0] * op.cycles)
                tdi.extend([0] * op.cycles)
                plain.append(future)
            elif isinstance(op, jtag.CaptureDr):
                self.__goto_rti(tms, tdi)
                # RTI -> Sel-DR -> Cap-DR -> Ex1-DR -> Pause-DR
                self.__emit_tms(tms, tdi, [1, 0, 1, 0])
                self.__state = self.STATE_PAUSE
                plain.append(future)
            elif isinstance(op, jtag.CaptureIr):
                self.__goto_rti(tms, tdi)
                # RTI -> Sel-DR -> Sel-IR -> Cap-IR -> Ex1-IR -> Pause-IR
                self.__emit_tms(tms, tdi, [1, 1, 0, 1, 0])
                self.__state = self.STATE_PAUSE
                plain.append(future)
            elif isinstance(op, jtag.Shift):
                self.__emit_shift(tms, tdi, shift_jobs, op, future, plain)
            else:
                future.set_exception(ValueError(
                    f"JtagJlink can't lower {type(op).__name__}"))

        if not tms:
            for f in plain:
                if not f.done():
                    f.set_result(None)
            return

        tdo_bytes = await self.__transport.jtag_io(
            _pack_bits(tms), _pack_bits(tdi), len(tms))
        tdo = _unpack_bits(tdo_bytes, len(tms))

        for future, start, end, length in shift_jobs:
            value = 0
            for i in range(length):
                if tdo[start + i]:
                    value |= 1 << i
            if not future.done():
                future.set_result(BitString(value, length))

        for f in plain:
            if not f.done():
                f.set_result(None)

    # -- FSM helpers -----------------------------------------------

    def __emit_tms(self, tms, tdi, bits):
        tms.extend(bits)
        tdi.extend([0] * len(bits))

    def __emit_pattern(self, tms, tdi, pattern: BitString):
        """Append a TMS bit pattern (TDI = 0 throughout)."""
        v = int(pattern)
        n = len(pattern)
        for i in range(n):
            tms.append((v >> i) & 1)
            tdi.append(0)

    def __goto_rti(self, tms, tdi):
        """Move from the current state to Run-Test/Idle."""
        if self.__state == self.STATE_RTI:
            return
        if self.__state == self.STATE_PAUSE:
            # Pause -> Exit2 -> Update -> RTI: TMS = 1, 1, 0
            self.__emit_tms(tms, tdi, [1, 1, 0])
        elif self.__state == self.STATE_RESET:
            # TLR -> RTI: TMS = 0
            self.__emit_tms(tms, tdi, [0])
        else:  # UNKNOWN — do a full TLR first.
            self.__emit_tms(tms, tdi, [1, 1, 1, 1, 1, 0])
        self.__state = self.STATE_RTI

    def __emit_shift(self, tms, tdi, shift_jobs, op, future, plain):
        """Lower a Shift op. Assumes we're in PAUSE state; will
        emit Pause -> Exit2 -> Shift transition on the way in and
        Exit1 -> Pause on the way out."""
        if self.__state != self.STATE_PAUSE:
            future.set_exception(RuntimeError(
                f"Shift from unexpected JTAG state {self.__state!r}"))
            return

        # Pause -> Exit2 -> Shift: TMS = 1, 0
        self.__emit_tms(tms, tdi, [1, 0])

        bit_start = len(tdi)
        v = int(op.tdi)
        n = len(op.tdi)

        if n == 0:
            future.set_result(BitString(0, 0) if op.read_tdo else None)
            return

        # First N-1 bits: TMS=0 (stay in Shift), TDI = bits.
        for i in range(n - 1):
            tms.append(0)
            tdi.append((v >> i) & 1)
        # Last bit: TMS=1 (-> Exit1), TDI = last bit.
        tms.append(1)
        tdi.append((v >> (n - 1)) & 1)
        bit_end = len(tdi)

        # Exit1 -> Pause: TMS=0
        self.__emit_tms(tms, tdi, [0])

        self.__state = self.STATE_PAUSE

        if op.read_tdo:
            shift_jobs.append((future, bit_start, bit_end, n))
        else:
            plain.append(future)
