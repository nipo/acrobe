"""JTAG ops → (TMS, TDI) bit-stream lowering.

Inverse of :class:`JtagTmsWalker`. Translates a batch of bit-level
:mod:`jtag` ops (Reset, Run, CaptureDr, CaptureIr, Shift) into a
(TMS, TDI) bit pair plus the per-Shift offsets needed to slice TDO
back out of the matching response.

Used by clients that talk to a remote peer at the TMS/TDI level —
Xilinx Virtual Cable, raw bit-bang adapters, future cousins.

Caveat: this lowering does not fit every transport. Adapters whose
command stream changes shape between control bits and shift payload
(FTDI MPSSE, JTAG-over-USB cables that bundle Cap/Shift/Exit into a
single primitive, …) need a transport-aware encoder. Use this when
the wire really is a homogeneous TMS/TDI stream.

State across calls
------------------
The encoder rests in one of TLR / RTI / PAUSE between calls. The
TAP-level state at end of one ``encode()`` call is the start state
of the next, so a caller can stream ops in arbitrary batches without
losing alignment with the peer's TAP state machine.
"""

from ..bitstring import BitString
from . import jtag


class JtagOpEncoder:
    """Stateful op → (TMS, TDI) encoder.

    Construct with the TAP state the peer is in (TLR by default —
    matches what the peer should be in after first connect or a fresh
    Reset). Then call :meth:`encode` for each batch.
    """

    TLR = "TLR"
    RTI = "RTI"
    PAUSE = "PAUSE"

    REST_STATES = (TLR, RTI, PAUSE)

    def __init__(self, initial_state: str = TLR) -> None:
        if initial_state not in self.REST_STATES:
            raise ValueError(
                f"initial_state must be one of {self.REST_STATES}, "
                f"got {initial_state!r}")
        self.__state = initial_state

    @property
    def state(self) -> str:
        return self.__state

    def encode(self, ops: list) -> tuple[BitString, BitString, list[tuple]]:
        """Lower ``ops`` to (TMS, TDI, slots).

        ``slots`` is a list of ``(shift_op, tdi_offset)`` pairs — one
        per :class:`jtag.Shift` in ``ops``, in input order. After the
        peer returns a TDO bit-vector of the same length as TMS/TDI,
        each shift's TDO is ``tdo[offset : offset + len(shift_op.tdi)]``.
        """
        tms = BitString()
        tdi = BitString()
        slots: list[tuple[jtag.Shift, int]] = []

        i = 0
        n = len(ops)
        while i < n:
            op = ops[i]
            next_op = ops[i + 1] if i + 1 < n else None

            if isinstance(op, (jtag.Reset, jtag.SwdToJtag)):
                self.__emit_tms_pattern(tms, tdi, op)

            elif isinstance(op, jtag.Run):
                self.__emit_run(tms, tdi, op)

            elif isinstance(op, jtag.CaptureDr):
                self.__emit_enter_capture(tms, tdi, ir=False)
                self.__emit_capture_exit(tms, tdi, ir=False, next_op=next_op)

            elif isinstance(op, jtag.CaptureIr):
                self.__emit_enter_capture(tms, tdi, ir=True)
                self.__emit_capture_exit(tms, tdi, ir=True, next_op=next_op)

            elif isinstance(op, jtag.Shift):
                self.___emit_shift(tms, tdi, op, slots, next_op)

            else:
                raise ValueError(
                    f"{type(self).__name__} cannot lower {type(op).__name__}; "
                    f"supported: Reset, SwdToJtag, Run, CaptureDr, CaptureIr, "
                    f"Shift")

            i += 1

        return tms, tdi, slots

    # ----- per-op emitters ------------------------------------------------

    def __emit_tms_pattern(self, tms: BitString, tdi: BitString, op) -> None:
        # Reset and SwdToJtag both expose a fixed TMS pattern via ``op.tms``
        # that ends with ≥5 ones, leaving the TAP in TLR. TDI is don't-care
        # — zero it for the whole stretch.
        tms.append(op.tms)
        tdi.append(0, len(op.tms))
        self.__state = self.TLR

    def __emit_run(self, tms: BitString, tdi: BitString,
                   op: jtag.Run) -> None:
        if self.__state == self.TLR:
            # TLR -[0]-> RTI. The Run cycles count from RTI.
            tms.append(0, 1)
            tdi.append(0, 1)
            self.__state = self.RTI
        elif self.__state == self.PAUSE:
            # Pause -[1]-> Ex2 -[1]-> Update -[0]-> RTI.
            tms.append(0b011, 3)
            tdi.append(0, 3)
            self.__state = self.RTI

        if op.cycles:
            tms.append(0, op.cycles)
            tdi.append(0, op.cycles)
        # Stay in RTI.

    def __emit_enter_capture(self, tms: BitString, tdi: BitString,
                             ir: bool) -> None:
        """Walk from current rest state to Capture-DR/IR. Leaves the
        TAP at the Capture state itself — the Capture→Shift or
        Capture→Exit1 edge is decided by ``__emit_capture_exit``."""
        if self.__state == self.RTI:
            if ir:
                # RTI -[1]-> SelDR -[1]-> SelIR -[0]-> CaptureIR
                tms.append(0b011, 3)
                tdi.append(0, 3)
            else:
                # RTI -[1]-> SelDR -[0]-> CaptureDR
                tms.append(0b01, 2)
                tdi.append(0, 2)
        elif self.__state == self.PAUSE:
            if ir:
                # Pause -[1]-> Ex2 -[1]-> Update -[1]-> SelDR -[1]-> SelIR
                #       -[0]-> CaptureIR  (5 bits)
                tms.append(0b01111, 5)
                tdi.append(0, 5)
            else:
                # Pause -[1]-> Ex2 -[1]-> Update -[1]-> SelDR -[0]-> CaptureDR
                tms.append(0b0111, 4)
                tdi.append(0, 4)
        elif self.__state == self.TLR:
            if ir:
                # TLR -[0]-> RTI -[1]-> SelDR -[1]-> SelIR -[0]-> CaptureIR
                tms.append(0b0110, 4)
                tdi.append(0, 4)
            else:
                # TLR -[0]-> RTI -[1]-> SelDR -[0]-> CaptureDR
                tms.append(0b010, 3)
                tdi.append(0, 3)
        else:
            raise RuntimeError(
                f"unexpected state at Capture entry: {self.__state}")

    def __emit_capture_exit(self, tms: BitString, tdi: BitString,
                            ir: bool, next_op) -> None:
        """Decide the path off the Capture state.

        Three cases:
        * Next op is a Shift: Capture -[0]-> Shift. State becomes
          "in shift", but we don't track it explicitly — the next
          iteration will hit the shift emitter, which assumes it
          owns the Capture→Shift edge bit it just received.
        * Next op is another Capture / Run: Capture -[1]-> Exit1
          -[1]-> Update -[1]-> SelDR -[0]-> Capture (4 bits, same
          shape regardless of DR/IR side). Conceptually we land back
          in the next op's Capture; we model this by treating ourselves
          as "in PAUSE" so the next iteration walks Pause→Capture
          (whose TMS sequence happens to be identical).
        * Otherwise (end of batch, or unknown follow-up): Capture
          -[1]-> Exit1 -[1]-> Pause. Rest in PAUSE.
        """
        if isinstance(next_op, jtag.Shift):
            # Capture -[0]-> Shift. Leaves the TAP in Shift state;
            # __emit_shift handles the actual shifting from there.
            tms.append(0, 1)
            tdi.append(0, 1)
            # Mark "we already emitted the Cap→Shift edge bit" via
            # the sentinel state below. __emit_shift recognises it.
            self.__state = "_PRE_SHIFT"
        elif isinstance(next_op, (jtag.CaptureDr, jtag.CaptureIr, jtag.Run)):
            # Identity trick: Capture-side path back to Capture is
            # 4 bits 0b0111, same as Pause→Capture. Pretend we're at
            # PAUSE so the next iteration walks the same TMS pattern
            # without us emitting anything here.
            self.__state = self.PAUSE
        else:
            # Capture -[1]-> Exit1 -[1]-> Pause.
            tms.append(0b01, 2)
            tdi.append(0, 2)
            self.__state = self.PAUSE

    def ___emit_shift(self, tms: BitString, tdi: BitString,
                     op: jtag.Shift, slots: list, next_op) -> None:
        """Shift op.tdi bits, then exit toward whatever follows.

        Expects the TAP to be in Shift state already (entered by a
        preceding CaptureDr/CaptureIr that emitted the Cap→Shift edge
        bit) or in PAUSE (back-to-back Shift ops within one batch —
        rare, but legal).
        """
        if self.__state == self.PAUSE:
            # Pause -[1]-> Ex2 -[0]-> Shift.
            tms.append(0b01, 2)
            tdi.append(0, 2)
        elif self.__state == "_PRE_SHIFT":
            # The Cap→Shift edge bit was already emitted by the
            # capture op's exit. We're in Shift now.
            pass
        elif self.__state == "_IN_SHIFT":
            # Continuation of a previous Shift — already in Shift state,
            # no entry transition needed.
            pass
        else:
            raise RuntimeError(
                f"Shift op not preceded by Capture/Pause: state={self.__state}")

        length = len(op.tdi)
        if length == 0:
            # Degenerate but legal: nothing to shift, just exit.
            self.__exit_shift(tms, tdi, next_op, last_tdi_bit=0,
                             skipped_payload=True)
            return

        # Record the start offset before payload bits.
        slots.append((op, len(tdi)))

        # All shift bits except the last: TMS=0 (stay in Shift),
        # TDI = op.tdi[:-1].
        if length > 1:
            tms.append(0, length - 1)
            tdi.append(op.tdi[:length - 1])

        # The last bit shares its clock edge with the Shift→Exit1
        # transition: TMS rises on that edge while TDI carries the
        # last payload bit. We fold that into a single appended bit
        # together with whatever exit path comes next.
        last_tdi = int(op.tdi[length - 1])
        self.__exit_shift(tms, tdi, next_op, last_tdi_bit=last_tdi,
                         skipped_payload=False)

    def __exit_shift(self, tms: BitString, tdi: BitString,
                     next_op, last_tdi_bit: int,
                     skipped_payload: bool) -> None:
        """Emit the Shift→exit transition.

        ``last_tdi_bit`` is the final TDI value clocked in on the
        Shift→Exit1 edge — for empty shifts this is 0 (no payload to
        emit, but we still need a single-bit "exit" if we're chaining).
        ``skipped_payload`` is True when we have no payload at all
        (zero-length shift): in that case the TAP is still in Shift
        with no bits clocked, and we exit by the same path but starting
        from "in Shift, no Cap edge yet".
        """
        if isinstance(next_op, jtag.Shift):
            # Stay in Shift across the boundary. The last payload bit
            # (if we have one) still needs to be clocked in, with
            # TMS=0 so the TAP stays in Shift for the next op to
            # continue from. Common pattern: Chain emits a Cap-DR
            # followed by pre/data/post Shifts in a single batch.
            if not skipped_payload:
                tms.append(0, 1)
                tdi.append(last_tdi_bit, 1)
            self.__state = "_IN_SHIFT"
            return

        if isinstance(next_op, (jtag.CaptureDr, jtag.CaptureIr, jtag.Run)):
            # Shift -[1]-> Exit1 -[1]-> Update -[0]-> RTI (3 bits).
            # First bit also carries the final TDI payload.
            if skipped_payload:
                # No payload bit fused; need a clean 3-bit exit. From
                # Shift, TMS=1 takes us to Exit1.
                tms.append(0b011, 3)
                tdi.append(0, 3)
            else:
                tms.append(0b011, 3)
                # First bit's TDI is the last payload bit; remaining
                # two are don't-care.
                tdi.append(last_tdi_bit | 0, 1)
                tdi.append(0, 2)
            self.__state = self.RTI
        else:
            # Default rest path: Shift -[1]-> Exit1 -[1]-> Pause.
            if skipped_payload:
                tms.append(0b01, 2)
                tdi.append(0, 2)
            else:
                tms.append(0b01, 2)
                tdi.append(last_tdi_bit, 1)
                tdi.append(0, 1)
            self.__state = self.PAUSE
