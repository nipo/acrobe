from __future__ import annotations

import asyncio

from .transport import ftdi_baudrate_divisor
from ...engine import Batcher
from ...bitstring import BitString, BitStringBase
from ...protocol import jtag


class JtagBitbang(Batcher):
    """JTAG interface using FTDI sync bitbang mode.

    Translates JTAG protocol operations into GPIO byte sequences
    for sync bitbang. Each JTAG clock cycle is two GPIO writes:
      1. TCK=0, TDI=bit, TMS=bit  (setup)
      2. TCK=1, TDI=bit, TMS=bit  (rising edge, target latches TDI)
    TDO is sampled from the read-back at TCK=1 positions.

    Pin positions are configurable (constructor args), unlike MPSSE
    which has fixed pin assignments.
    """

    STATE_UNKNOWN = "unknown"
    STATE_RESET = "reset"
    STATE_RTI = "rti"
    STATE_PAUSE = "pause"

    def __init__(self, transport, *, tck, tms, tdi, tdo, logger):
        super().__init__()
        self._transport = transport
        self._tck = tck
        self._tms = tms
        self._tdi = tdi
        self._tdo = tdo
        self._tdo_mask = 1 << tdo
        self._state = self.STATE_UNKNOWN
        self._baudrate = None
        self._baudrate_dirty = False
        self.logger = logger

    def freq_update(self, freq):
        """Set JTAG clock frequency via FTDI bitbang baud rate.

        Each JTAG cycle is 2 GPIO bytes, so baudrate = 2 × freq.
        Returns actual achieved frequency (never exceeds requested).
        """
        if freq is None:
            return None
        target_baudrate = freq * 2
        actual_baudrate, _ = ftdi_baudrate_divisor(target_baudrate)
        if actual_baudrate != self._baudrate:
            self._baudrate = actual_baudrate
            self._baudrate_dirty = True
        return actual_baudrate / 2

    def _cycle(self, buf, tms_bit, tdi_bit):
        """Append two GPIO bytes for one JTAG clock cycle.

        Returns the index of the rising-edge byte (where TDO is valid).
        """
        val = 0
        if tms_bit:
            val |= 1 << self._tms
        if tdi_bit:
            val |= 1 << self._tdi
        buf.append(val)              # TCK=0 (setup)
        buf.append(val | (1 << self._tck))  # TCK=1 (clock)
        return len(buf) - 1  # index of rising-edge byte

    def _emit_tms_pattern(self, buf, tms, tdi=0):
        """Emit TMS bit pattern. TDI held constant."""
        for i in range(len(tms)):
            self._cycle(buf, tms[i], tdi)

    def _emit_run(self, buf, cycles):
        """Emit Run-Test/Idle transition and clock cycles."""
        if self._state == self.STATE_PAUSE:
            # Pause -> Exit2 -> Update
            self._cycle(buf, 1, 0)
            self._cycle(buf, 1, 0)
            self._state = self.STATE_RTI
        elif self._state == self.STATE_RESET:
            self._state = self.STATE_RTI

        if self._state == self.STATE_RTI:
            # TMS=0 enters/stays in RTI
            for _ in range(max(cycles, 1)):
                self._cycle(buf, 0, 0)
        else:
            # Unknown state: reset first
            for _ in range(5):
                self._cycle(buf, 1, 0)
            self._cycle(buf, 0, 0)
            self._state = self.STATE_RTI

    def _emit_capture_dr(self, buf):
        """Emit RTI/Update -> Capture-DR -> Exit1-DR -> Pause-DR."""
        if self._state == self.STATE_PAUSE:
            # Pause -> Exit2 -> Update
            self._cycle(buf, 1, 0)
            self._cycle(buf, 1, 0)
        elif self._state != self.STATE_RTI:
            raise ValueError(f"CaptureDr from unexpected state: {self._state}")
        # RTI/Update -> Select-DR -> Capture-DR -> Exit1-DR -> Pause-DR
        # TMS pattern: 1, 0, 1, 0
        self._cycle(buf, 1, 0)
        self._cycle(buf, 0, 0)
        self._cycle(buf, 1, 0)
        self._cycle(buf, 0, 0)
        self._state = self.STATE_PAUSE

    def _emit_capture_ir(self, buf):
        """Emit RTI/Update -> Capture-IR -> Exit1-IR -> Pause-IR."""
        if self._state == self.STATE_PAUSE:
            # Pause -> Exit2 -> Update
            self._cycle(buf, 1, 0)
            self._cycle(buf, 1, 0)
        elif self._state != self.STATE_RTI:
            raise ValueError(f"CaptureIr from unexpected state: {self._state}")
        # RTI/Update -> Sel-DR -> Sel-IR -> Cap-IR -> Ex1-IR -> Pause-IR
        # TMS pattern: 1, 1, 0, 1, 0
        self._cycle(buf, 1, 0)
        self._cycle(buf, 1, 0)
        self._cycle(buf, 0, 0)
        self._cycle(buf, 1, 0)
        self._cycle(buf, 0, 0)
        self._state = self.STATE_PAUSE

    def _emit_shift(self, buf, op):
        """Emit Pause -> Shift -> data -> Exit1 -> Pause.

        Returns list of read-back byte indices where TDO is valid
        (one per TDI bit), or None if read_tdo is False.
        """
        assert self._state == self.STATE_PAUSE

        tdi = op.tdi
        read = op.read_tdo

        if not isinstance(tdi, BitStringBase):
            raise TypeError(f"Expected BitString for tdi, got {type(tdi)}")

        cycle_count = len(tdi)
        if cycle_count == 0:
            return None

        # Pause -> Exit2 -> Shift
        self._cycle(buf, 1, 0)  # Pause -> Exit2
        self._cycle(buf, 0, 0)  # Exit2 -> Shift

        tdo_indices = [] if read else None

        # Shift all but the last bit (TMS=0)
        for i in range(cycle_count - 1):
            idx = self._cycle(buf, 0, tdi[i])
            if read:
                tdo_indices.append(idx)

        # Last bit via TMS=1: shifts the bit AND transitions to Exit1
        idx = self._cycle(buf, 1, tdi[-1])
        if read:
            tdo_indices.append(idx)

        # Exit1 -> Pause
        self._cycle(buf, 0, 0)

        # State remains PAUSE
        return tdo_indices

    async def flush_ops(self, batch):
        """Translate JTAG operations to bitbang GPIO bytes and execute."""
        if self._baudrate_dirty:
            self._baudrate_dirty = False
            await self._transport.set_baudrate(self._baudrate)
        self.logger.log(5, "JTAG-BB batch: %s", [op for op, _ in batch])
        buf = bytearray()
        # Track Shift ops that need TDO extraction
        tdo_entries = []  # (batch_index, [read-back byte indices])

        for idx, (op, future) in enumerate(batch):
            if isinstance(op, (jtag.Reset, jtag.SwdToJtag)):
                self._emit_tms_pattern(buf, op.tms)
                self._state = self.STATE_RESET

            elif isinstance(op, jtag.Run):
                self._emit_run(buf, op.cycles)

            elif isinstance(op, jtag.CaptureDr):
                self._emit_capture_dr(buf)

            elif isinstance(op, jtag.CaptureIr):
                self._emit_capture_ir(buf)

            elif isinstance(op, jtag.Shift):
                tdo_indices = self._emit_shift(buf, op)
                if tdo_indices is not None:
                    tdo_entries.append((idx, tdo_indices))

            else:
                raise ValueError(f"Unknown JTAG op: {type(op).__name__}")

        # Execute: in sync bitbang, every byte written produces a byte read
        if buf:
            data = bytes(buf)
            _, rsp = await asyncio.gather(
                self._transport.write(data),
                self._transport.read(len(data)))
        else:
            rsp = b""

        # Extract TDO from read-back at rising-edge positions
        for batch_idx, indices in tdo_entries:
            op = batch[batch_idx][0]
            tdo = BitString()
            for byte_idx in indices:
                bit = 1 if (rsp[byte_idx] & self._tdo_mask) else 0
                tdo.append(bit, 1)
            op.tdo = tdo

        # Resolve all batch futures
        for op, future in batch:
            future.set_result(op)
