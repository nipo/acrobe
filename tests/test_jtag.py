import asyncio
import pytest
from acrobe.protocol.jtag import (
    Shift, CaptureDr, CaptureIr, Reset, Run, SwdToJtag,
    Dr, Instruction, TapDr, TapInstruction, InstructionRegistry,
    Tap, Chain, ChainContext, OpenChain, _DynamicInstruction,
    _TapShift, _TapRun, JtagInterface,
)
from acrobe.bitstring import BitString


# -- Mock Interfaces --

class MockInterface(JtagInterface):
    """Records all bit-level ops posted to it. Resolves Shift.tdo with
    zero data so Tap-level reads see all-zero TDO."""

    def __init__(self):
        super().__init__(name="mock")
        self.ops = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Shift) and op.read_tdo:
                op.tdo = BitString(0, len(op.tdi))
            future.set_result(op)


def _make_chain(iface):
    chain = Chain()
    iface.child_add(chain)
    return chain


def _make_tap(iface, base=Tap, idcode=0, irlen=4):
    """Test helper: build chain under iface and add a single tap."""
    chain = _make_chain(iface)
    return chain.tap_add(idcode, irlen=irlen, base=base)


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
        assert len(s.tms) == 71


class TestInstructionRegistry:
    def test_dr_spawn(self):
        dr = Dr(length=32)
        tap = _make_tap(MockInterface())
        spawned = dr._spawn("TEST_DR", tap)
        assert isinstance(spawned, TapDr)
        assert spawned.length == 32
        assert spawned.name == "TEST_DR"

    def test_instruction_spawn(self):
        class MyTap(Tap):
            ID_REG = Dr(length=32)
            IDCODE = Instruction(0x0e, "ID_REG")

        tap = _make_tap(MockInterface(), base=MyTap)
        assert isinstance(tap.ID_REG, TapDr)
        assert isinstance(tap.IDCODE, TapInstruction)
        assert tap.IDCODE.dr is tap.ID_REG
        assert tap.IDCODE.dr.length == 32

    def test_bypass_always_present(self):
        tap = _make_tap(MockInterface())
        assert isinstance(tap.BYPASS, TapInstruction)
        assert isinstance(tap.BYPASS_REG, TapDr)
        assert tap.BYPASS_REG.length == 1

    def test_instructions_iterator(self):
        class MyTap(Tap):
            ID_REG = Dr(length=32)
            IDCODE = Instruction(0x0e, "ID_REG")

        tap = _make_tap(MockInterface(), base=MyTap)
        instrs = list(tap.instructions())
        names = {i.name for i in instrs}
        assert "BYPASS" in names
        assert "IDCODE" in names

    def test_instruction_int(self):
        class MyTap(Tap):
            IDCODE = Instruction(0x0e)

        tap = _make_tap(MockInterface(), base=MyTap)
        assert int(tap.IDCODE) == 0x0e

    def test_instruction_ir_masked(self):
        class MyTap(Tap):
            WIDE = Instruction(0xff)

        tap = _make_tap(MockInterface(), base=MyTap)
        assert int(tap.WIDE) == 0x0f


class TestTapBasics:
    def test_tap_init(self):
        tap = Tap(idcode=0x12345678, irlen=4)
        assert tap.irlen == 4
        assert tap.idcode == 0x12345678
        assert "TAP" in tap.name

    def test_tap_init_no_idcode(self):
        tap = Tap(irlen=4, name="test")
        assert tap.name == "test"

    def test_chain_context_geometry(self):
        chain = Chain()
        tap = chain.tap_add(0, irlen=4)
        ctx = chain.context(tap)
        assert ctx.ir_pre == 0
        assert ctx.ir_post == 0
        assert ctx.dr_pre == 0
        assert ctx.dr_post == 0


class TestTapShift:
    @pytest.mark.asyncio
    async def test_read_instruction(self):
        """Calling an instruction with a DR length reads TDO."""
        class MyTap(Tap):
            ID_REG = Dr(length=32)
            IDCODE = Instruction(0x0e, "ID_REG")

        tap = _make_tap(MockInterface(), base=MyTap)
        result = await tap.IDCODE()
        assert result is not None
        assert isinstance(result, BitString)
        assert len(result) == 32

    @pytest.mark.asyncio
    async def test_write_instruction(self):
        """Calling an instruction with tdi sends data."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        tap = _make_tap(MockInterface(), base=MyTap)
        result = await tap.DATA(0xab)
        assert result is not None

    @pytest.mark.asyncio
    async def test_write_no_read(self):
        """Calling with read_tdo=False returns None."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        tap = _make_tap(MockInterface(), base=MyTap)
        result = await tap.DATA(0xab, read_tdo=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_ir_tracking(self):
        """IR shift is only emitted when IR changes (across batches)."""
        class MyTap(Tap):
            REG_A = Dr(length=8)
            REG_B = Dr(length=8)
            INST_A = Instruction(0x01, "REG_A")
            INST_B = Instruction(0x02, "REG_B")

        iface = MockInterface()
        tap = _make_tap(iface, base=MyTap)

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
        chain = _make_chain(iface)
        tap = chain.tap_add(0, irlen=4, base=MyTap)
        ctx = chain.context(tap)
        ctx.ir_pre = 3
        ctx.ir_post = 5

        await tap.DATA(0)

        ir_shift = None
        for i, op in enumerate(iface.ops):
            if isinstance(op, CaptureIr):
                ir_shift = iface.ops[i + 1]
                break
        assert ir_shift is not None
        assert isinstance(ir_shift, Shift)

        # Total IR length: 3 (pre) + 4 (irlen) + 5 (post) = 12 bits
        assert len(ir_shift.tdi) == 12
        assert int(ir_shift.tdi[:3]) == 0b111
        assert int(ir_shift.tdi[3:7]) == 0x05
        assert int(ir_shift.tdi[7:12]) == 0b11111

    @pytest.mark.asyncio
    async def test_dr_padding(self):
        """DR shift should include pre/post bypass bits."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        iface = MockInterface()
        chain = _make_chain(iface)
        tap = chain.tap_add(0, irlen=4, base=MyTap)
        ctx = chain.context(tap)
        ctx.dr_pre = 2
        ctx.dr_post = 3

        await tap.DATA(0xab)

        capture_dr_ops = [op for op in iface.ops if isinstance(op, CaptureDr)]
        assert len(capture_dr_ops) == 1

        dr_start = next(i for i, op in enumerate(iface.ops) if isinstance(op, CaptureDr))
        dr_shifts = [op for op in iface.ops[dr_start + 1:] if isinstance(op, Shift)]

        # 3 shifts: pre (2 bits), data (8 bits), post (3 bits)
        assert len(dr_shifts) == 3
        assert len(dr_shifts[0].tdi) == 2
        assert len(dr_shifts[1].tdi) == 8
        assert len(dr_shifts[2].tdi) == 3

    @pytest.mark.asyncio
    async def test_batching(self):
        """Multiple ops posted before await should batch."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            INST_A = Instruction(0x01, "DATA_REG")
            INST_B = Instruction(0x02, "DATA_REG")

        tap = _make_tap(MockInterface(), base=MyTap)

        f1 = tap.INST_A(0x11)
        f2 = tap.INST_B(0x22)
        r1, r2 = await asyncio.gather(f1, f2)
        assert r1 is not None
        assert r2 is not None

    @pytest.mark.asyncio
    async def test_run(self):
        """tap.run() posts a Run op to the interface."""
        iface = MockInterface()
        tap = _make_tap(iface)
        await tap.run(10)

        run_ops = [op for op in iface.ops if isinstance(op, Run)]
        assert len(run_ops) == 1
        assert run_ops[0].cycles == 10

    @pytest.mark.asyncio
    async def test_dr_type_accessible(self):
        """Dr.type is an accessible attribute but TDO is always BitString."""
        class MyTap(Tap):
            ID_REG = Dr(length=32, type=int)
            IDCODE = Instruction(0x0e, "ID_REG")

        tap = _make_tap(MockInterface(), base=MyTap)
        assert tap.ID_REG.type is int
        result = await tap.IDCODE()
        assert isinstance(result, BitString)


class TestIrStatus:
    @pytest.mark.asyncio
    async def test_ir_status_returns_bitstring(self):
        tap = _make_tap(MockInterface(), irlen=6)
        result = await tap.ir_status()
        assert isinstance(result, BitString)
        assert len(result) == 6

    @pytest.mark.asyncio
    async def test_ir_status_emits_capture_ir(self):
        iface = MockInterface()
        tap = _make_tap(iface, irlen=6)
        await tap.ir_status()
        assert any(isinstance(op, CaptureIr) for op in iface.ops)

    @pytest.mark.asyncio
    async def test_ir_status_shifts_bypass(self):
        iface = MockInterface()
        tap = _make_tap(iface, irlen=6)
        await tap.ir_status()
        shifts = [op for op in iface.ops if isinstance(op, Shift)]
        assert len(shifts) == 1
        assert shifts[0].read_tdo is True
        assert int(shifts[0].tdi) == 0x3f  # 6 bits all ones

    @pytest.mark.asyncio
    async def test_ir_status_updates_current_ir(self):
        """After ir_status(), the tap's cached IR is BYPASS (all 1s)."""
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x05, "DATA_REG")

        iface = MockInterface()
        chain = _make_chain(iface)
        tap = chain.tap_add(0, irlen=6, base=MyTap)

        await tap.DATA(0)
        assert chain.context(tap).current_ir == 0x05

        iface.ops.clear()
        await tap.ir_status()
        assert chain.context(tap).current_ir == 0x3f  # all ones, BYPASS

    @pytest.mark.asyncio
    async def test_ir_status_with_chain_padding(self):
        iface = MockInterface()
        chain = _make_chain(iface)
        tap = chain.tap_add(0, irlen=6)
        ctx = chain.context(tap)
        ctx.ir_pre = 4
        ctx.ir_post = 5

        await tap.ir_status()
        shifts = [op for op in iface.ops if isinstance(op, Shift)]
        # 3 shifts: pre (4 bits, no read), data (6 bits, read), post (5 bits, no read)
        assert len(shifts) == 3
        assert len(shifts[0].tdi) == 4
        assert shifts[0].read_tdo is False
        assert int(shifts[0].tdi) == 0xf
        assert len(shifts[1].tdi) == 6
        assert shifts[1].read_tdo is True
        assert int(shifts[1].tdi) == 0x3f
        assert len(shifts[2].tdi) == 5
        assert shifts[2].read_tdo is False
        assert int(shifts[2].tdi) == 0x1f

    @pytest.mark.asyncio
    async def test_ir_status_no_padding_when_zero(self):
        iface = MockInterface()
        tap = _make_tap(iface, irlen=4)
        await tap.ir_status()
        shifts = [op for op in iface.ops if isinstance(op, Shift)]
        assert len(shifts) == 1
        assert shifts[0].read_tdo is True


class TestDynamicInstruction:
    @pytest.mark.asyncio
    async def test_dynamic_read(self):
        tap = _make_tap(MockInterface())
        dyn = tap.ir(0x05, dr_length=32)
        result = await dyn()
        assert result is not None
        assert len(result) == 32

    @pytest.mark.asyncio
    async def test_dynamic_write(self):
        tap = _make_tap(MockInterface())
        dyn = tap.ir(0x05, dr_length=8)
        result = await dyn(0xab)
        assert result is not None

    @pytest.mark.asyncio
    async def test_dynamic_write_bitstring(self):
        tap = _make_tap(MockInterface())
        dyn = tap.ir(0x05)
        result = await dyn(BitString(0xab, 8))
        assert result is not None

    def test_dynamic_no_length_int_raises(self):
        tap = _make_tap(MockInterface())
        dyn = tap.ir(0x05)
        with pytest.raises(ValueError, match="Cannot determine"):
            dyn(42)

    def test_dynamic_ir_masked(self):
        tap = _make_tap(MockInterface())
        dyn = tap.ir(0xff)
        assert dyn._ir_value == 0x0f

    def test_dynamic_repr(self):
        tap = _make_tap(MockInterface())
        dyn = tap.ir(0x05)
        assert "DynamicInstruction" in repr(dyn)


class TestChain:
    def test_single_tap(self):
        chain = Chain()
        tap = chain.tap_add(0x12345678, irlen=4)

        assert isinstance(tap, Tap)
        assert tap.irlen == 4
        ctx = chain.context(tap)
        assert ctx.ir_pre == 0
        assert ctx.ir_post == 0
        assert ctx.dr_pre == 0
        assert ctx.dr_post == 0
        assert chain.total_irlen == 4
        assert chain.total_drlen == 1

    def test_two_taps(self):
        chain = Chain()
        tap1 = chain.tap_add(0x11111111, irlen=4)
        tap2 = chain.tap_add(0x22222222, irlen=5)

        assert chain.total_irlen == 9
        assert chain.total_drlen == 2

        c1 = chain.context(tap1)
        c2 = chain.context(tap2)
        assert c1.ir_pre == 0
        assert c1.ir_post == 5
        assert c1.dr_pre == 0
        assert c1.dr_post == 1

        assert c2.ir_pre == 4
        assert c2.ir_post == 0
        assert c2.dr_pre == 1
        assert c2.dr_post == 0

    def test_three_taps(self):
        chain = Chain()
        tap1 = chain.tap_add(0x11111111, irlen=4)
        tap2 = chain.tap_add(0x22222222, irlen=5)
        tap3 = chain.tap_add(0x33333333, irlen=6)

        assert chain.total_irlen == 15
        assert chain.total_drlen == 3

        c1, c2, c3 = chain.context(tap1), chain.context(tap2), chain.context(tap3)
        assert c1.ir_pre == 0
        assert c1.ir_post == 11
        assert c1.dr_pre == 0
        assert c1.dr_post == 2

        assert c2.ir_pre == 4
        assert c2.ir_post == 6
        assert c2.dr_pre == 1
        assert c2.dr_post == 1

        assert c3.ir_pre == 9
        assert c3.ir_post == 0
        assert c3.dr_pre == 2
        assert c3.dr_post == 0

    def test_tap_is_child(self):
        chain = Chain()
        tap = chain.tap_add(0x12345678, irlen=4)
        assert tap in chain.children

    def test_registered_tap_class(self):
        """Chain.tap_add uses Tap.db to find registered Tap subclasses."""
        class SpecialTap(Tap):
            ID_REG = Dr(length=32)
            IDCODE = Instruction(0x0e, "ID_REG")

        Tap.db.register(0xdeadbeef)(SpecialTap)
        try:
            chain = Chain()
            tap = chain.tap_add(0xdeadbeef, irlen=4)
            assert isinstance(tap, SpecialTap)
            assert isinstance(tap.IDCODE, TapInstruction)
        finally:
            Tap.db._registry.clear()

    def test_repr(self):
        chain = Chain()
        assert "Chain" in repr(chain)


class TestErrorCases:
    def test_read_no_dr_length(self):
        class MyTap(Tap):
            NO_DR = Instruction(0x01)

        tap = _make_tap(MockInterface(), base=MyTap)
        with pytest.raises(ValueError, match="Cannot determine"):
            tap.NO_DR(read_tdo=True)

    def test_write_int_no_dr_length(self):
        class MyTap(Tap):
            NO_DR = Instruction(0x01)

        tap = _make_tap(MockInterface(), base=MyTap)
        with pytest.raises(ValueError, match="Cannot determine"):
            tap.NO_DR(42)

    def test_write_bad_type(self):
        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        tap = _make_tap(MockInterface(), base=MyTap)
        with pytest.raises(TypeError, match="tdi must be"):
            tap.DATA("bad")

    @pytest.mark.asyncio
    async def test_interface_error_propagates(self):
        """Errors from the interface propagate to Tap futures."""
        class FailInterface(JtagInterface):
            async def flush_ops(self, batch):
                raise IOError("USB error")

        class MyTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x01, "DATA_REG")

        tap = _make_tap(FailInterface(), base=MyTap)
        with pytest.raises(IOError, match="USB error"):
            await tap.DATA(0)


# -- Chain Discover --

class ChainSimulator(JtagInterface):
    """Simulates a JTAG chain for testing Chain.discover().

    Models a chain of devices with known IDCODEs and IR lengths.
    Handles Reset, Capture-DR/IR, and Shift operations with proper
    shift register behavior.
    """

    def __init__(self, devices):
        super().__init__(name="sim")
        self.devices = devices
        self._reg_val = 0
        self._reg_len = 0
        self._bypass = False
        self._in_ir = False

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Reset):
                self._bypass = False
                self._in_ir = False
            elif isinstance(op, CaptureDr):
                self._in_ir = False
                if self._bypass:
                    self._reg_val = 0
                    self._reg_len = len(self.devices)
                else:
                    val, pos = 0, 0
                    for idcode, _ in self.devices:
                        val |= idcode << pos
                        pos += 32
                    self._reg_val = val
                    self._reg_len = pos
            elif isinstance(op, CaptureIr):
                self._in_ir = True
                val, pos = 0, 0
                for _, irlen in self.devices:
                    val |= 1 << pos
                    pos += irlen
                self._reg_val = val
                self._reg_len = pos
            elif isinstance(op, Shift):
                self._do_shift(op)
            future.set_result(op)

    def _do_shift(self, op):
        L = self._reg_len
        N = len(op.tdi)
        tdi_val = int(op.tdi)

        if op.read_tdo:
            if N <= L:
                op.tdo = BitString(self._reg_val & ((1 << N) - 1), N)
            else:
                tdo_val = (self._reg_val | (tdi_val << L)) & ((1 << N) - 1)
                op.tdo = BitString(tdo_val, N)

        if L > 0 and N >= L:
            new_val = (tdi_val >> (N - L)) & ((1 << L) - 1)
            self._reg_val = new_val
            if self._in_ir and new_val == (1 << L) - 1:
                self._bypass = True


class TestChainDiscover:
    @pytest.mark.asyncio
    async def test_single_device(self):
        sim = ChainSimulator([(0x24001093, 6)])
        chain = _make_chain(sim)
        await chain.discover()

        assert len(chain.children) == 1
        tap = chain.children[0]
        assert tap.idcode == 0x24001093
        assert tap.irlen == 6

    @pytest.mark.asyncio
    async def test_two_devices(self):
        sim = ChainSimulator([(0x24001093, 6), (0x0ba00477, 4)])
        chain = _make_chain(sim)
        await chain.discover()

        assert len(chain.children) == 2
        assert chain.children[0].idcode == 0x24001093
        assert chain.children[0].irlen == 6
        assert chain.children[1].idcode == 0x0ba00477
        assert chain.children[1].irlen == 4

    @pytest.mark.asyncio
    async def test_three_devices(self):
        sim = ChainSimulator([
            (0x11111111, 4),
            (0x22222223, 5),
            (0x33333333, 6),
        ])
        chain = _make_chain(sim)
        await chain.discover()

        assert len(chain.children) == 3
        assert chain.children[0].irlen == 4
        assert chain.children[1].irlen == 5
        assert chain.children[2].irlen == 6

    @pytest.mark.asyncio
    async def test_chain_geometry(self):
        sim = ChainSimulator([(0x11111111, 4), (0x22222223, 5)])
        chain = _make_chain(sim)
        await chain.discover()

        tap0, tap1 = chain.children
        c0, c1 = chain.context(tap0), chain.context(tap1)
        assert c0.ir_pre == 0
        assert c0.ir_post == 5
        assert c0.dr_pre == 0
        assert c0.dr_post == 1
        assert c1.ir_pre == 4
        assert c1.ir_post == 0
        assert c1.dr_pre == 1
        assert c1.dr_post == 0

    @pytest.mark.asyncio
    async def test_unknown_device_generic_tap(self):
        sim = ChainSimulator([(0xdeadbeef, 5)])
        chain = _make_chain(sim)
        await chain.discover()

        tap = chain.children[0]
        assert type(tap) is Tap
        assert tap.idcode == 0xdeadbeef
        assert tap.irlen == 5

    @pytest.mark.asyncio
    async def test_registered_tap_used(self):
        class KnownTap(Tap):
            irlen = 4

        Tap.db.register(0xaabbccdd)(KnownTap)
        try:
            sim = ChainSimulator([(0xaabbccdd, 4)])
            chain = _make_chain(sim)
            await chain.discover()
            assert isinstance(chain.children[0], KnownTap)
        finally:
            Tap.db._registry.clear()

    @pytest.mark.asyncio
    async def test_open_chain_stuck_low(self):
        class StuckLow(JtagInterface):
            def __init__(self):
                super().__init__(name="stuck-low")

            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        op.tdo = BitString(0, len(op.tdi))
                    future.set_result(op)

        chain = _make_chain(StuckLow())
        with pytest.raises(OpenChain, match="stuck low"):
            await chain.discover()

    @pytest.mark.asyncio
    async def test_open_chain_stuck_high(self):
        class StuckHigh(JtagInterface):
            def __init__(self):
                super().__init__(name="stuck-high")

            async def flush_ops(self, batch):
                for op, future in batch:
                    if isinstance(op, Shift) and op.read_tdo:
                        op.tdo = BitString(-1, len(op.tdi))
                    future.set_result(op)

        chain = _make_chain(StuckHigh())
        with pytest.raises(OpenChain, match="stuck high"):
            await chain.discover()
