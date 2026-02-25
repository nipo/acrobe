import asyncio
import pytest
from crobe_async.protocol.jtag import (
    Shift, CaptureDr, CaptureIr, Reset, Run, SwdToJtag,
    Dr, Instruction, TapDr, TapInstruction, InstructionRegistry,
    Tap, Chain, _DynamicInstruction, _TapShift, _TapRun,
)
from crobe_async.bitstring import BitString
from crobe_async.engine import Batcher


# -- Mock Interface --

class MockInterface(Batcher):
    """Records all posted ops and resolves Shift.tdo with dummy data."""

    def __init__(self):
        super().__init__()
        self.ops = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Shift) and op.read_tdo:
                op.tdo = BitString(0, len(op.tdi))
            future.set_result(op)


class TestJtagOps:
    def test_shift_repr(self):
        s = Shift(BitString(0xab, 8), read_tdo=True)
        assert "Shift" in repr(s)

    def test_capture_dr(self):
        assert "CaptureDr" in repr(CaptureDr())

    def test_capture_ir(self):
        assert "CaptureIr" in repr(CaptureIr())

    def test_reset_default(self):
        r = Reset()
        assert len(r.tms) >= 5
        # All TMS bits should be 1
        assert int(r.tms) == (1 << len(r.tms)) - 1

    def test_reset_custom_count(self):
        r = Reset(count=10)
        assert len(r.tms) == 10

    def test_reset_min_5(self):
        r = Reset(count=2)
        assert len(r.tms) == 5

    def test_run(self):
        r = Run(100)
        assert r.cycles == 100

    def test_swd_to_jtag(self):
        s = SwdToJtag()
        # 50 + 16 + 5 = 71 bits
        assert len(s.tms) == 71


class TestInstructionRegistry:
    def test_dr_spawn(self):
        dr = Dr(length=32)
        tap = Tap(MockInterface(), irlen=4)
        spawned = dr._spawn("TEST_DR", tap)
        assert isinstance(spawned, TapDr)
        assert spawned.length == 32
        assert spawned.name == "TEST_DR"

    def test_instruction_spawn(self):
        class MyTap(Tap):
            ID_REG = Dr(length=32)
            IDCODE = Instruction(0x0e, "ID_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)

        assert isinstance(tap.ID_REG, TapDr)
        assert isinstance(tap.IDCODE, TapInstruction)
        assert tap.IDCODE.dr is tap.ID_REG
        assert tap.IDCODE.dr.length == 32

    def test_bypass_always_present(self):
        tap = Tap(MockInterface(), irlen=4)
        assert isinstance(tap.BYPASS, TapInstruction)
        assert isinstance(tap.BYPASS_REG, TapDr)
        assert tap.BYPASS_REG.length == 1

    def test_instructions_iterator(self):
        class MyTap(Tap):
            ID_REG = Dr(length=32)
            IDCODE = Instruction(0x0e, "ID_REG")

        tap = MyTap(MockInterface(), irlen=4)
        instrs = list(tap.instructions())
        names = {i.name for i in instrs}
        assert "BYPASS" in names
        assert "IDCODE" in names

    def test_instruction_int(self):
        class MyTap(Tap):
            IDCODE = Instruction(0x0e)

        tap = MyTap(MockInterface(), irlen=4)
        assert int(tap.IDCODE) == 0x0e

    def test_instruction_ir_masked(self):
        class MyTap(Tap):
            WIDE = Instruction(0xff)

        tap = MyTap(MockInterface(), irlen=4)
        # 0xff masked to 4 bits = 0x0f
        assert int(tap.WIDE) == 0x0f


class TestTapBasics:
    def test_tap_init(self):
        tap = Tap(MockInterface(), idcode=0x12345678, irlen=4)
        assert tap.irlen == 4
        assert tap.idcode == 0x12345678
        assert "TAP" in tap.name

    def test_tap_init_no_idcode(self):
        tap = Tap(MockInterface(), irlen=4, name="test")
        assert tap.name == "test"

    def test_position_set(self):
        tap = Tap(MockInterface(), irlen=4)
        tap.position_set(3, 1, 5, 2)
        assert tap.ir_pre == 3
        assert tap.dr_pre == 1
        assert tap.ir_post == 5
        assert tap.dr_post == 2


class TestTapShift:
    @pytest.mark.asyncio
    async def test_read_instruction(self):
        """Calling an instruction with a DR length reads TDO."""
        class MyTap(Tap):
            ID_REG = Dr(length=32)
            IDCODE = Instruction(0x0e, "ID_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        result = await tap.IDCODE()
        # MockInterface returns all-zero TDO
        assert result is not None
        assert isinstance(result, BitString)
        assert len(result) == 32

    @pytest.mark.asyncio
    async def test_write_instruction(self):
        """Calling an instruction with tdi sends data."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        result = await tap.DATA(0xab)
        # read_tdo defaults to True when tdi is provided
        assert result is not None

    @pytest.mark.asyncio
    async def test_write_no_read(self):
        """Calling with read_tdo=False returns None."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        result = await tap.DATA(0xab, read_tdo=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_ir_tracking(self):
        """IR shift is only emitted when IR changes."""
        class MyTap(Tap):
            REG_A = Dr(length=8)
            REG_B = Dr(length=8)
            INST_A = Instruction(0x01, "REG_A")
            INST_B = Instruction(0x02, "REG_B")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)

        await tap.INST_A(0)
        capture_ir_count_1 = sum(1 for op in iface.ops if isinstance(op, CaptureIr))
        assert capture_ir_count_1 == 1

        # Same instruction again - no new CaptureIr
        await tap.INST_A(0)
        capture_ir_count_2 = sum(1 for op in iface.ops if isinstance(op, CaptureIr))
        assert capture_ir_count_2 == 1

        # Different instruction - new CaptureIr
        await tap.INST_B(0)
        capture_ir_count_3 = sum(1 for op in iface.ops if isinstance(op, CaptureIr))
        assert capture_ir_count_3 == 2

    @pytest.mark.asyncio
    async def test_ir_data_format(self):
        """IR shift data should include pre/post padding."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x05, "DATA_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        tap.position_set(ir_pre=3, dr_pre=0, ir_post=5, dr_post=0)

        await tap.DATA(0)

        # Find the IR shift: the shift immediately after CaptureIr
        ir_shift = None
        for i, op in enumerate(iface.ops):
            if isinstance(op, CaptureIr):
                ir_shift = iface.ops[i + 1]
                break
        assert ir_shift is not None
        assert isinstance(ir_shift, Shift)

        # Total IR length: 3 (pre) + 4 (irlen) + 5 (post) = 12 bits
        assert len(ir_shift.tdi) == 12
        # Pre-padding is all 1s (BYPASS)
        assert int(ir_shift.tdi[:3]) == 0b111
        # IR value (0x05 in 4 bits)
        assert int(ir_shift.tdi[3:7]) == 0x05
        # Post-padding is all 1s (BYPASS)
        assert int(ir_shift.tdi[7:12]) == 0b11111

    @pytest.mark.asyncio
    async def test_dr_padding(self):
        """DR shift should include pre/post bypass bits."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        tap.position_set(ir_pre=0, dr_pre=2, ir_post=0, dr_post=3)

        await tap.DATA(0xab)

        # Should have: CaptureDr, DR pre-shift (2 bits), data shift (8 bits), DR post-shift (3 bits)
        capture_dr_ops = [op for op in iface.ops if isinstance(op, CaptureDr)]
        assert len(capture_dr_ops) == 1

        shift_ops = [op for op in iface.ops if isinstance(op, Shift)
                     and not any(isinstance(prev, CaptureIr)
                                 for prev in iface.ops[:iface.ops.index(op)]
                                 if isinstance(prev, CaptureIr) and
                                 iface.ops.index(prev) > max(
                                     (iface.ops.index(o) for o in iface.ops[:iface.ops.index(op)]
                                      if isinstance(o, CaptureDr)),
                                     default=-1))]
        # Simpler: count shifts after the CaptureDr
        dr_start = next(i for i, op in enumerate(iface.ops) if isinstance(op, CaptureDr))
        dr_shifts = [op for op in iface.ops[dr_start+1:] if isinstance(op, Shift)]

        # 3 shifts: pre (2 bits), data (8 bits), post (3 bits)
        assert len(dr_shifts) == 3
        assert len(dr_shifts[0].tdi) == 2   # pre
        assert len(dr_shifts[1].tdi) == 8   # data
        assert len(dr_shifts[2].tdi) == 3   # post

    @pytest.mark.asyncio
    async def test_batching(self):
        """Multiple ops posted before await should batch."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            INST_A = Instruction(0x01, "DATA_REG")
            INST_B = Instruction(0x02, "DATA_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)

        f1 = tap.INST_A(0x11)
        f2 = tap.INST_B(0x22)
        r1, r2 = await asyncio.gather(f1, f2)
        assert r1 is not None
        assert r2 is not None

    @pytest.mark.asyncio
    async def test_run(self):
        """tap.run() posts a Run op to the interface."""
        iface = MockInterface()
        tap = Tap(iface, irlen=4)
        await tap.run(10)

        run_ops = [op for op in iface.ops if isinstance(op, Run)]
        assert len(run_ops) == 1
        assert run_ops[0].cycles == 10

    @pytest.mark.asyncio
    async def test_postprocess(self):
        """DR type postprocessing is applied to TDO."""
        class MyTap(Tap):
            ID_REG = Dr(length=32, type=int)
            IDCODE = Instruction(0x0e, "ID_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        result = await tap.IDCODE()
        assert isinstance(result, int)


class TestDynamicInstruction:
    @pytest.mark.asyncio
    async def test_dynamic_read(self):
        """tap.ir(value, length) creates a readable dynamic instruction."""
        iface = MockInterface()
        tap = Tap(iface, irlen=4)
        dyn = tap.ir(0x05, dr_length=32)
        result = await dyn()
        assert result is not None
        assert len(result) == 32

    @pytest.mark.asyncio
    async def test_dynamic_write(self):
        """Dynamic instruction with tdi."""
        iface = MockInterface()
        tap = Tap(iface, irlen=4)
        dyn = tap.ir(0x05, dr_length=8)
        result = await dyn(0xab)
        assert result is not None

    @pytest.mark.asyncio
    async def test_dynamic_write_bitstring(self):
        """Dynamic instruction with BitString tdi."""
        iface = MockInterface()
        tap = Tap(iface, irlen=4)
        dyn = tap.ir(0x05)
        result = await dyn(BitString(0xab, 8))
        assert result is not None

    def test_dynamic_no_length_int_raises(self):
        """Dynamic instruction without length can't determine shift from int."""
        iface = MockInterface()
        tap = Tap(iface, irlen=4)
        dyn = tap.ir(0x05)
        with pytest.raises(ValueError, match="Cannot determine"):
            dyn(42)

    def test_dynamic_ir_masked(self):
        """Dynamic instruction IR value is masked to irlen."""
        iface = MockInterface()
        tap = Tap(iface, irlen=4)
        dyn = tap.ir(0xff)
        assert dyn._ir_value == 0x0f

    def test_dynamic_repr(self):
        iface = MockInterface()
        tap = Tap(iface, irlen=4)
        dyn = tap.ir(0x05)
        assert "DynamicInstruction" in repr(dyn)


class TestChain:
    def test_single_tap(self):
        iface = MockInterface()
        chain = Chain(iface)
        tap = chain.tap_add(0x12345678, irlen=4)

        assert isinstance(tap, Tap)
        assert tap.irlen == 4
        assert tap.ir_pre == 0
        assert tap.ir_post == 0
        assert tap.dr_pre == 0
        assert tap.dr_post == 0
        assert chain.total_irlen == 4
        assert chain.total_drlen == 1

    def test_two_taps(self):
        iface = MockInterface()
        chain = Chain(iface)

        tap1 = chain.tap_add(0x11111111, irlen=4)
        tap2 = chain.tap_add(0x22222222, irlen=5)

        assert chain.total_irlen == 9
        assert chain.total_drlen == 2

        # tap1 was added first (position 0)
        assert tap1.ir_pre == 0
        assert tap1.ir_post == 5  # tap2's irlen
        assert tap1.dr_pre == 0
        assert tap1.dr_post == 1

        # tap2 was added second (position 1)
        assert tap2.ir_pre == 4  # tap1's irlen
        assert tap2.ir_post == 0
        assert tap2.dr_pre == 1
        assert tap2.dr_post == 0

    def test_three_taps(self):
        iface = MockInterface()
        chain = Chain(iface)

        tap1 = chain.tap_add(0x11111111, irlen=4)
        tap2 = chain.tap_add(0x22222222, irlen=5)
        tap3 = chain.tap_add(0x33333333, irlen=6)

        assert chain.total_irlen == 15
        assert chain.total_drlen == 3

        # tap1: first in chain
        assert tap1.ir_pre == 0
        assert tap1.ir_post == 11  # 5 + 6
        assert tap1.dr_pre == 0
        assert tap1.dr_post == 2

        # tap2: middle
        assert tap2.ir_pre == 4
        assert tap2.ir_post == 6
        assert tap2.dr_pre == 1
        assert tap2.dr_post == 1

        # tap3: last
        assert tap3.ir_pre == 9  # 4 + 5
        assert tap3.ir_post == 0
        assert tap3.dr_pre == 2
        assert tap3.dr_post == 0

    def test_tap_is_child(self):
        iface = MockInterface()
        chain = Chain(iface)
        tap = chain.tap_add(0x12345678, irlen=4)
        assert tap in chain.children

    def test_registered_tap_class(self):
        """Chain.tap_add uses Tap.db to find registered Tap subclasses."""
        class SpecialTap(Tap):
            ID_REG = Dr(length=32)
            IDCODE = Instruction(0x0e, "ID_REG")

        Tap.db.register(0xdeadbeef)(SpecialTap)
        try:
            iface = MockInterface()
            chain = Chain(iface)
            tap = chain.tap_add(0xdeadbeef, irlen=4)
            assert isinstance(tap, SpecialTap)
            assert isinstance(tap.IDCODE, TapInstruction)
        finally:
            # Clean up registration
            Tap.db._registry.clear()

    def test_repr(self):
        iface = MockInterface()
        chain = Chain(iface)
        assert "Chain" in repr(chain)


class TestErrorCases:
    def test_read_no_dr_length(self):
        """Instruction with no DR length can't determine read size."""
        class MyTap(Tap):
            NO_DR = Instruction(0x01)

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        with pytest.raises(ValueError, match="Cannot determine"):
            tap.NO_DR(read_tdo=True)

    def test_write_int_no_dr_length(self):
        """Can't write int without knowing DR length."""
        class MyTap(Tap):
            NO_DR = Instruction(0x01)

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        with pytest.raises(ValueError, match="Cannot determine"):
            tap.NO_DR(42)

    def test_write_bad_type(self):
        """tdi must be int, BitString, or None."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        iface = MockInterface()
        tap = MyTap(iface, irlen=4)
        with pytest.raises(TypeError, match="tdi must be"):
            tap.DATA("bad")

    @pytest.mark.asyncio
    async def test_interface_error_propagates(self):
        """Errors from the interface propagate to Tap futures."""
        class FailInterface(Batcher):
            async def flush_ops(self, batch):
                raise IOError("USB error")

        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        tap = MyTap(FailInterface(), irlen=4)
        with pytest.raises(IOError, match="USB error"):
            await tap.DATA(0)
