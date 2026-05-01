"""JTAG TAP-state walker driven by a (TMS, TDI) bit stream.

Used by remote bit-bang protocols (JoP, Xilinx VCD, …) where the network
peer drives JTAG at the TMS/TDI level and we need to translate that
stream into acrobe :class:`JtagInterface` ops.

The walker mirrors the JTAG state machine per input bit. On state
transitions, it decides what the just-completed run of bits represented
(a DR shift, an IR shift, an idle run, a TAP reset, a Capture-only) and
posts the corresponding interface op. Captured TDO bits are slotted back
into a same-length output vector aligned with the input.

Caveats
-------
acrobe's :class:`Shift` op enters Shift-DR/IR via Pause and exits via
Pause. The actual hardware spends a few extra TCKs in Exit2/Exit1/Pause
states around each shift compared to a literal walk of the TMS stream.
This is invisible to the protocol-level peer because:

* TDO is only captured during the actual shift bits (the JoP and VCD
  protocols only enable capture during shifts in practice);
* The TAP doesn't change state otherwise — Pause is a JTAG-spec idle.

If a peer ever requires capturing TDO during the literal TCK count of
the input (e.g. with JoP's CMD_WRITE_TDO_ENABLE_FIFO duration covering
non-Shift bits), bits outside Shift segments are filled with zeros.
"""

import asyncio
import logging

from ..bitstring import BitString
from . import jtag


_logger = logging.getLogger("jtag.walker")


class JtagTmsWalker:
    """Process (TMS, TDI) bit pairs against the JTAG TAP state machine
    and post resulting :mod:`jtag` ops to a :class:`JtagInterface`.

    State indexing intentionally collapses the DR/IR mirror sides into
    a single set of states (Capture/Shift/…); we track which side via
    :attr:`_in_ir` set on entry to Capture.
    """

    TLR = 0      # Test-Logic-Reset
    RTI = 1      # Run-Test/Idle
    SEL_DR = 2   # Select-DR-Scan
    SEL_IR = 3   # Select-IR-Scan
    CAPTURE = 4
    SHIFT = 5
    EXIT1 = 6
    PAUSE = 7
    EXIT2 = 8
    UPDATE = 9

    NAMES = ["TLR", "RTI", "Sel-DR", "Sel-IR",
             "Capture", "Shift", "Exit1", "Pause", "Exit2", "Update"]

    # NEXT_STATE[tms][current] -> next
    NEXT_STATE = (
        # TMS=0
        (RTI, RTI, CAPTURE, CAPTURE, SHIFT, SHIFT, PAUSE, PAUSE, SHIFT, RTI),
        # TMS=1
        (TLR, SEL_DR, SEL_IR, TLR, EXIT1, EXIT1, UPDATE, EXIT2, UPDATE, SEL_DR),
    )

    def __init__(self, interface):
        self._interface = interface
        self._state = self.TLR
        self._in_ir = False

    @property
    def state(self) -> int:
        return self._state

    @property
    def state_name(self) -> str:
        return self.NAMES[self._state]

    async def process(self, tms, tdi) -> BitString:
        """Walk one batch of TMS+TDI bits, drive the interface, return TDO.

        ``tms`` and ``tdi`` must be equal-length :class:`BitString`. The
        returned BitString has the same length; bits captured during a
        Shift segment are populated, all other positions are zero.
        """
        if len(tms) != len(tdi):
            raise ValueError(
                f"tms/tdi length mismatch: {len(tms)} vs {len(tdi)}")
        n = len(tms)
        if n == 0:
            return BitString()

        # If the previous call ended mid-Shift (e.g. peer is sending one
        # JoP shift command per bit), the very first bit of this call
        # belongs to the same Shift run. Start counting from offset 0
        # so it isn't silently dropped.
        shift_start: int | None = (0 if self._state == self.SHIFT else None)
        run_start: int | None = (0 if self._state == self.RTI else None)
        # (future, start, end) — TDO output range that this Shift covers.
        slots: list[tuple[asyncio.Future, int, int]] = []
        # All futures we post, so we can gather and flush them before returning
        # even when there are no Shift ops.
        futures: list[asyncio.Future] = []

        def post(op):
            fut = self._interface.post(op)
            futures.append(fut)
            return fut

        for i in range(n):
            tms_bit = int(tms[i])
            next_state = self.NEXT_STATE[tms_bit][self._state]

            # Leaving Shift: the current bit is already the boundary TMS=1
            # bit; acrobe's Shift consumes the entire segment (including its
            # last bit) and handles the Exit1 transition internally.
            if self._state == self.SHIFT and next_state != self.SHIFT:
                segment = tdi[shift_start:i + 1]
                fut = post(
                    jtag.Shift(segment, read_tdo=True))
                slots.append((fut, shift_start, i + 1))
                shift_start = None

            # Leaving RTI: emit the run we accumulated.
            if (self._state == self.RTI and next_state != self.RTI
                    and run_start is not None):
                cycles = i - run_start
                if cycles > 0:
                    post(jtag.Run(cycles))
                run_start = None

            # Leaving TLR towards real work: post a Reset to align the
            # acrobe interface with the peer's view.
            if self._state == self.TLR and next_state != self.TLR:
                post(jtag.Reset())

            # Entering Capture: choose the right capture op and remember
            # which side we're on.
            if next_state == self.CAPTURE:
                self._in_ir = (self._state == self.SEL_IR)
                if self._in_ir:
                    post(jtag.CaptureIr())
                else:
                    post(jtag.CaptureDr())

            # Entering Shift: the bit at position i is the Cap→Shift (or
            # Ex2→Shift) state-transition edge. Per JTAG spec, no shift
            # action happens on that edge — shifting only fires on TCKs
            # whose pre-edge state is already Shift. So the segment of
            # actual shift bits starts at i+1, not i. Forwarding bit i
            # to acrobe.Shift would consume one extra bit from the chain
            # before the peer's capture window opens, off-setting every
            # subsequent capture.
            if next_state == self.SHIFT and self._state != self.SHIFT:
                shift_start = i + 1
                # Bundled-pattern warning: real Quartus traffic uses a
                # separate JoP command for the entry edge, so the call
                # ends with i+1 == n and no further bits. If we see more
                # input bits after the entry, the JoP peer combined the
                # entry with shifts in one command. The session-level
                # capture loop iterates over all input positions, so a
                # capture window covering this command would mis-decrement
                # by 1 at the entry bit's position. Flag it so a future
                # tool that changes pattern is visible in logs.
                if i + 1 < n:
                    _logger.warning(
                        "JoP shift command bundles a Cap→Shift / Ex2→Shift "
                        "entry edge with %d subsequent shift bit(s). "
                        "If the peer scheduled a capture window covering "
                        "this command, captured TDO will be off-by-one. "
                        "Real Quartus traffic uses separate commands for "
                        "the entry edge — investigate the new pattern.",
                        n - (i + 1))

            # Entering RTI: start a new run counter.
            if next_state == self.RTI and self._state != self.RTI:
                run_start = i

            self._state = next_state

        # End-of-input: if still inside a run-state, flush it.
        # Skip when shift_start ≥ n — happens when the only Shift event in
        # the call was the entry edge (which is not a shift action).
        if (self._state == self.SHIFT
                and shift_start is not None and shift_start < n):
            # Stream ended inside Shift — peer is drip-feeding bits and
            # hasn't sent the boundary TMS=1 yet. Acrobe.Shift exits to
            # Pause, but the chain's shift register stays consistent
            # because subsequent Pause→Ex2→Shift transitions don't
            # capture or reset.
            segment = tdi[shift_start:n]
            fut = post(jtag.Shift(segment, read_tdo=True))
            slots.append((fut, shift_start, n))

        if (self._state == self.RTI
                and run_start is not None and run_start < n):
            post(jtag.Run(n - run_start))

        # Drive the interface: await every posted future so the Batcher
        # actually flushes (Run/Reset/CaptureDr/CaptureIr have no result
        # to inspect, but we must still wait for them to complete before
        # returning, otherwise ordering with subsequent calls is broken).
        if futures:
            await asyncio.gather(*futures)

        # Reassemble TDO bit-aligned with input.
        if not slots:
            return BitString(0, n)
        tdo = BitString()
        cursor = 0
        for fut, start, end in slots:
            if start > cursor:
                tdo += BitString(0, start - cursor)
            shift_op = fut.result()
            tdo += shift_op.tdo
            cursor = end
        if cursor < n:
            tdo += BitString(0, n - cursor)
        return tdo
