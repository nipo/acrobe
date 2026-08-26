from __future__ import annotations

import asyncio

from .transport import ftdi_baudrate_divisor
from ...bitstring import BitString, BitStringBase
from ...protocol import jtag


class JtagBitbang(jtag.JtagInterface):
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

    def __init__(self, transport, *, tck, tms, tdi, tdo, name="jtag"):
        super().__init__(name=name)
        self.__transport = transport
        self.__tck = tck
        self.__tms = tms
        self.__tdi = tdi
        self.__tdo = tdo
        self.__tdo_mask = 1 << tdo
        self.__state = self.STATE_UNKNOWN
        self.__baudrate = None
        self.__baudrate_dirty = False

    def freq_update(self, freq):
        """Set JTAG clock frequency via FTDI bitbang baud rate.

        Each JTAG cycle is 2 GPIO bytes, so baudrate = 2 × freq.
        Returns actual achieved frequency (never exceeds requested).
        """
        if freq is None:
            return None
        target_baudrate = freq * 2
        actual_baudrate, _ = ftdi_baudrate_divisor(target_baudrate)
        if actual_baudrate != self.__baudrate:
            self.__baudrate = actual_baudrate
            self.__baudrate_dirty = True
        return actual_baudrate / 2

    def __cycle(self, buf, tms_bit, tdi_bit):
        """Append two GPIO bytes for one JTAG clock cycle.

        Returns the index of the rising-edge byte (where TDO is valid).
        """
        val = 0
        if tms_bit:
            val |= 1 << self.__tms
        if tdi_bit:
            val |= 1 << self.__tdi
        buf.append(val)              # TCK=0 (setup)
        buf.append(val | (1 << self.__tck))  # TCK=1 (clock)
        return len(buf) - 1  # index of rising-edge byte

    def __emit_tms_pattern(self, buf, tms, tdi=0):
        """Emit TMS bit pattern. TDI held constant."""
        for i in range(len(tms)):
            self.__cycle(buf, tms[i], tdi)

    def __emit_run(self, buf, cycles):
        """Emit Run-Test/Idle transition and clock cycles."""
        if self.__state == self.STATE_PAUSE:
            # Pause -> Exit2 -> Update
            self.__cycle(buf, 1, 0)
            self.__cycle(buf, 1, 0)
            self.__state = self.STATE_RTI
        elif self.__state == self.STATE_RESET:
            self.__state = self.STATE_RTI

        if self.__state == self.STATE_RTI:
            # TMS=0 enters/stays in RTI
            for _ in range(max(cycles, 1)):
                self.__cycle(buf, 0, 0)
        else:
            # Unknown state: reset first
            for _ in range(5):
                self.__cycle(buf, 1, 0)
            self.__cycle(buf, 0, 0)
            self.__state = self.STATE_RTI

    def __emit_capture_dr(self, buf):
        """Emit RTI/Update -> Capture-DR -> Exit1-DR -> Pause-DR."""
        if self.__state == self.STATE_PAUSE:
            # Pause -> Exit2 -> Update
            self.__cycle(buf, 1, 0)
            self.__cycle(buf, 1, 0)
        elif self.__state != self.STATE_RTI:
            raise ValueError(f"CaptureDr from unexpected state: {self.__state}")
        # RTI/Update -> Select-DR -> Capture-DR -> Exit1-DR -> Pause-DR
        # TMS pattern: 1, 0, 1, 0
        self.__cycle(buf, 1, 0)
        self.__cycle(buf, 0, 0)
        self.__cycle(buf, 1, 0)
        self.__cycle(buf, 0, 0)
        self.__state = self.STATE_PAUSE

    def __emit_capture_ir(self, buf):
        """Emit RTI/Update -> Capture-IR -> Exit1-IR -> Pause-IR."""
        if self.__state == self.STATE_PAUSE:
            # Pause -> Exit2 -> Update
            self.__cycle(buf, 1, 0)
            self.__cycle(buf, 1, 0)
        elif self.__state != self.STATE_RTI:
            raise ValueError(f"CaptureIr from unexpected state: {self.__state}")
        # RTI/Update -> Sel-DR -> Sel-IR -> Cap-IR -> Ex1-IR -> Pause-IR
        # TMS pattern: 1, 1, 0, 1, 0
        self.__cycle(buf, 1, 0)
        self.__cycle(buf, 1, 0)
        self.__cycle(buf, 0, 0)
        self.__cycle(buf, 1, 0)
        self.__cycle(buf, 0, 0)
        self.__state = self.STATE_PAUSE

    def __emit_shift(self, buf, op):
        """Emit Pause -> Shift -> data -> Exit1 -> Pause.

        Returns list of read-back byte indices where TDO is valid
        (one per TDI bit), or None if read_tdo is False.
        """
        assert self.__state == self.STATE_PAUSE

        tdi = op.tdi
        read = op.read_tdo

        if not isinstance(tdi, BitStringBase):
            raise TypeError(f"Expected BitString for tdi, got {type(tdi)}")

        cycle_count = len(tdi)
        if cycle_count == 0:
            return None

        # Pause -> Exit2 -> Shift
        self.__cycle(buf, 1, 0)  # Pause -> Exit2
        self.__cycle(buf, 0, 0)  # Exit2 -> Shift

        tdo_indices = [] if read else None

        # Shift all but the last bit (TMS=0)
        for i in range(cycle_count - 1):
            idx = self.__cycle(buf, 0, tdi[i])
            if read:
                tdo_indices.append(idx)

        # Last bit via TMS=1: shifts the bit AND transitions to Exit1
        idx = self.__cycle(buf, 1, tdi[-1])
        if read:
            tdo_indices.append(idx)

        # Exit1 -> Pause
        self.__cycle(buf, 0, 0)

        # State remains PAUSE
        return tdo_indices

    async def flush_ops(self, batch):
        """Translate JTAG operations to bitbang GPIO bytes and execute."""
        if self.__baudrate_dirty:
            self.__baudrate_dirty = False
            await self.__transport.set_baudrate(self.__baudrate)
        self.logger.log(5, "JTAG-BB batch: %s", [op for op, _ in batch])
        buf = bytearray()
        # Track Shift ops that need TDO extraction
        tdo_entries = []  # (batch_index, [read-back byte indices])

        for idx, (op, future) in enumerate(batch):
            if isinstance(op, (jtag.Reset, jtag.SwdToJtag)):
                self.__emit_tms_pattern(buf, op.tms)
                self.__state = self.STATE_RESET

            elif isinstance(op, jtag.Run):
                self.__emit_run(buf, op.cycles)

            elif isinstance(op, jtag.CaptureDr):
                self.__emit_capture_dr(buf)

            elif isinstance(op, jtag.CaptureIr):
                self.__emit_capture_ir(buf)

            elif isinstance(op, jtag.Shift):
                tdo_indices = self.__emit_shift(buf, op)
                if tdo_indices is not None:
                    tdo_entries.append((idx, tdo_indices))

            else:
                raise ValueError(f"Unknown JTAG op: {type(op).__name__}")

        # Execute: in sync bitbang, every byte written produces a byte read
        if buf:
            data = bytes(buf)
            _, rsp = await asyncio.gather(
                self.__transport.write(data),
                self.__transport.read(len(data)))
        else:
            rsp = b""

        # Extract TDO from read-back at rising-edge positions
        captured: dict[int, BitString] = {}
        for batch_idx, indices in tdo_entries:
            tdo = BitString()
            for byte_idx in indices:
                bit = 1 if (rsp[byte_idx] & self.__tdo_mask) else 0
                tdo.append(bit, 1)
            captured[batch_idx] = tdo

        # Resolve futures with the captured BitString (or None for ops
        # with no read).
        for idx, (_, future) in enumerate(batch):
            future.set_result(captured.get(idx))
