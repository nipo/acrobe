"""Tests for :class:`JtagTmsWalker`.

We use a fake JtagInterface that records every posted op and synthesises
TDO bits as ``~tdi`` for shifts. That gives us deterministic round-trip
checks without needing actual hardware.
"""

import pytest

from acrobe.bitstring import BitString
from acrobe.protocol import jtag
from acrobe.protocol.jtag_walker import JtagTmsWalker


class _RecordingInterface(jtag.JtagInterface):
    """Minimal JtagInterface that records posted ops in order and
    resolves Shift futures with the bitwise complement of tdi."""

    def __init__(self):
        super().__init__()
        self.ops: list = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, jtag.Shift):
                inv = (1 << len(op.tdi)) - 1 - int(op.tdi)
                future.set_result(BitString(inv, len(op.tdi)))
            else:
                future.set_result(None)


def _bs_from_bits(bits: list[int]) -> BitString:
    out = BitString()
    for b in bits:
        out += BitString(b, 1)
    return out


class TestStateTransitions:
    @pytest.mark.asyncio
    async def test_idle_run_only(self):
        # Walker starts in TLR. TMS=00000000 — bit 0 transitions TLR→RTI
        # (run_start=0), bits 1..7 stay in RTI. Run cycles count from the
        # entry bit (RTI was entered at bit 0), giving 8 total — that
        # matches acrobe's Run(N) which emits N TCKs at TMS=0 starting
        # from the RTI entry transition.
        iface = _RecordingInterface()
        walker = JtagTmsWalker(iface)
        tms = BitString(0, 8)  # all zeros
        tdi = BitString(0, 8)
        await walker.process(tms, tdi)
        kinds = [type(op).__name__ for op in iface.ops]
        assert kinds == ["Reset", "Run"]
        assert iface.ops[1].cycles == 8
        assert walker.state == JtagTmsWalker.RTI

    @pytest.mark.asyncio
    async def test_tlr_5_ones_then_rti(self):
        # TMS=11111,0 keeps us in TLR for 5 bits, then drops to RTI.
        iface = _RecordingInterface()
        walker = JtagTmsWalker(iface)
        tms = _bs_from_bits([1, 1, 1, 1, 1, 0])
        tdi = BitString(0, 6)
        await walker.process(tms, tdi)
        # We start in TLR; TMS=1 keeps us there. The first TMS=0 moves
        # us to RTI, emitting Reset on the TLR→RTI transition, then we
        # spend exactly one bit in RTI before end-of-input → Run(1).
        kinds = [type(op).__name__ for op in iface.ops]
        assert kinds == ["Reset", "Run"]
        assert iface.ops[1].cycles == 1


class TestDrShift:
    @pytest.mark.asyncio
    async def test_simple_dr_scan(self):
        # Scan a 4-bit DR with TDI=0xA. Path:
        #   TLR(0)→RTI(1)→Sel-DR(0)→Cap-DR(0)→Shift-DR
        #   shift 4 bits with TMS=0, last bit TMS=1 → Exit1-DR
        #   (1)→Update-DR(0)→RTI
        iface = _RecordingInterface()
        walker = JtagTmsWalker(iface)
        # state walk:
        #  i=0: TLR, tms=0 → RTI
        #  i=1: RTI, tms=1 → Sel-DR
        #  i=2: Sel-DR, tms=0 → Cap-DR
        #  i=3: Cap-DR, tms=0 → Shift-DR        (start of shift)
        #  i=4: Shift-DR, tms=0 → Shift-DR
        #  i=5: Shift-DR, tms=0 → Shift-DR
        #  i=6: Shift-DR, tms=0 → Shift-DR
        #  i=7: Shift-DR, tms=1 → Exit1-DR      (end of shift, 4 bits)
        #  i=8: Exit1-DR, tms=1 → Update-DR
        #  i=9: Update-DR, tms=0 → RTI
        tms = _bs_from_bits([0, 1, 0, 0, 0, 0, 0, 1, 1, 0])
        tdi = _bs_from_bits([0, 0, 0, 0, 0, 1, 0, 1, 0, 0])
        # The Cap-DR→Shift-DR transition (bit 3) is a state transition,
        # not a shift action — it must NOT be forwarded to acrobe. The
        # actual shift bits are bits 4..7 (the boundary at bit 7 IS a
        # shift action, since its pre-edge state is Shift-DR).

        tdo = await walker.process(tms, tdi)

        kinds = [type(op).__name__ for op in iface.ops]
        # Expected:
        #   Reset      (TLR→RTI)
        #   Run(1)     (1-cycle RTI before TMS=1 leaves it for Sel-DR)
        #   CaptureDr  (entering Capture)
        #   Shift      (4-bit segment: bits 4..7 inclusive of boundary)
        #   Run(1)     (1-cycle RTI before end of stream)
        assert kinds == ["Reset", "Run", "CaptureDr", "Shift", "Run"]
        assert iface.ops[1].cycles == 1
        assert iface.ops[4].cycles == 1

        shift_op = iface.ops[3]
        assert len(shift_op.tdi) == 4
        expected_tdi = tdi[4:8]
        assert shift_op.tdi == expected_tdi

        # TDO of length 10, only positions 4..7 populated with ~tdi[4:8].
        assert len(tdo) == 10
        for i in (0, 1, 2, 3, 8, 9):
            assert int(tdo[i]) == 0
        assert int(tdo[4:8]) == ((1 << 4) - 1) - int(tdi[4:8])


class TestIrShift:
    @pytest.mark.asyncio
    async def test_ir_scan_chooses_capture_ir(self):
        # Path: TLR→RTI→Sel-DR→Sel-IR→Cap-IR→Shift-IR…
        iface = _RecordingInterface()
        walker = JtagTmsWalker(iface)
        # bits: 0 (TLR→RTI), 1 (RTI→Sel-DR), 1 (Sel-DR→Sel-IR),
        #       0 (Sel-IR→Cap-IR), 0 (Cap-IR→Shift-IR),
        #       0,0,1 (3 shifts + boundary), 1 (Ex1-IR→Up-IR), 0 (Up-IR→RTI)
        tms = _bs_from_bits([0, 1, 1, 0, 0, 0, 0, 1, 1, 0])
        tdi = _bs_from_bits([0, 0, 0, 0, 0, 1, 1, 0, 0, 0])
        await walker.process(tms, tdi)
        kinds = [type(op).__name__ for op in iface.ops]
        assert kinds == ["Reset", "Run", "CaptureIr", "Shift", "Run"]


class TestRunAccumulation:
    @pytest.mark.asyncio
    async def test_run_between_two_scans(self):
        # Two DR scans separated by RTI cycles.
        iface = _RecordingInterface()
        walker = JtagTmsWalker(iface)
        # Build: enter RTI, do 1-bit DR shift, return to RTI, idle 5 cycles,
        # do another 1-bit DR shift, end in RTI.
        # TMS  = [0, 1,0,0, 1, 1,0,    0,0,0,0,0,    1,0,0, 1, 1,0]
        # We want: leaving the first scan returns us to RTI for some run,
        # before the second scan starts.

        # path:
        #  0: TLR→RTI  (Reset)
        #  1: RTI→Sel-DR
        #  2: Sel-DR→Cap-DR  (CaptureDr)
        #  3: Cap-DR→Shift-DR (start)
        #  4: Shift-DR→Exit1-DR (1-bit shift)         (Shift, len=1)
        #  5: Exit1-DR→Update-DR
        #  6: Update-DR→RTI
        #  7..11: RTI→RTI 5 cycles                    (Run from when we
        #                                              entered RTI at 6,
        #                                              flushed when we leave
        #                                              at 12; cycles=12-6=6)
        #  12: RTI→Sel-DR
        #  13: Sel-DR→Cap-DR (CaptureDr)
        #  14: Cap-DR→Shift-DR
        #  15: Shift-DR→Exit1-DR (1-bit shift)        (Shift, len=1)
        #  16: Exit1-DR→Update-DR
        #  17: Update-DR→RTI
        tms = _bs_from_bits(
            [0, 1, 0, 0, 1, 1, 0,
             0, 0, 0, 0, 0,
             1, 0, 0, 1, 1, 0])
        tdi = BitString(0, len(tms))
        await walker.process(tms, tdi)
        kinds = [type(op).__name__ for op in iface.ops]
        # Reset, Run(1) (TLR→RTI single bit), CaptureDr, Shift,
        # Run(6) (RTI for bits 6..11), CaptureDr, Shift,
        # Run(1) (final RTI bit before end of stream).
        assert kinds == [
            "Reset", "Run", "CaptureDr", "Shift",
            "Run", "CaptureDr", "Shift", "Run",
        ]
        assert iface.ops[1].cycles == 1
        # Run between the two scans: entered RTI at bit 6, left at bit 12.
        assert iface.ops[4].cycles == 12 - 6
        assert iface.ops[7].cycles == 1


class TestBatching:
    @pytest.mark.asyncio
    async def test_consecutive_one_bit_shifts_dont_drop_bits(self):
        # Quartus's chain detection sends one 1-bit shift JoP command at
        # a time, all with TMS=0 to stay in Shift state. The walker is
        # called once per command; each call enters with state=SHIFT and
        # sees a single TMS=0 bit. The bit must NOT be silently dropped.
        iface = _RecordingInterface()
        walker = JtagTmsWalker(iface)

        # First, walk into Shift-DR with a self-contained call.
        # Path: TLR(0)→RTI(1)→Sel-DR(0)→Cap-DR(0)→Shift-DR
        await walker.process(
            _bs_from_bits([0, 1, 0, 0]), BitString(0, 4))
        assert walker.state == JtagTmsWalker.SHIFT

        # The Cap-DR→Shift-DR entry edge is a state transition, not a
        # shift action — no Shift op should have been emitted yet.
        assert not any(isinstance(op, jtag.Shift) for op in iface.ops)

        # Now drip-feed three single-bit shifts (each its own call).
        for _ in range(3):
            await walker.process(_bs_from_bits([0]),
                                  _bs_from_bits([1]))

        shift_ops = [op for op in iface.ops if isinstance(op, jtag.Shift)]
        # Each per-call invocation must produce exactly one 1-bit Shift —
        # without the fix, bits are silently dropped here.
        assert len(shift_ops) == 3
        for op in shift_ops:
            assert len(op.tdi) == 1
            assert int(op.tdi) == 1

    @pytest.mark.asyncio
    async def test_walker_state_persists_across_calls(self):
        iface = _RecordingInterface()
        walker = JtagTmsWalker(iface)
        # First call: TLR→RTI, then start a DR scan up to (but not into)
        # Shift-DR.
        # TMS: 0 (TLR→RTI), 1 (RTI→Sel-DR), 0 (Sel-DR→Cap-DR)
        await walker.process(_bs_from_bits([0, 1, 0]), BitString(0, 3))
        assert walker.state == JtagTmsWalker.CAPTURE

        # Second call: continue. Cap-DR→Shift-DR, 3 shifts, exit, finish.
        # TMS: 0 (Cap-DR→Shift-DR), 0,0,1 (3 shift bits, last=boundary),
        #      1 (Ex1→Up), 0 (Up→RTI)
        await walker.process(
            _bs_from_bits([0, 0, 0, 1, 1, 0]),
            _bs_from_bits([0, 1, 1, 0, 0, 0]))
        kinds = [type(op).__name__ for op in iface.ops]
        # First call:  Reset, Run(1), CaptureDr.
        # Second call: Shift (3 bits — bit 0 is the Cap→Shift entry, the
        # actual shifts are bits 1..3 incl. boundary), Run(1).
        assert kinds == ["Reset", "Run", "CaptureDr", "Shift", "Run"]
        assert len(iface.ops[3].tdi) == 3
