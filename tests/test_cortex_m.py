"""Tests for Cortex-M SCS run-control, FPB, CortexMCore, CortexMDebuggable."""

import asyncio
import pytest

from acrobe.component.arm.coresight.fpb import Fpb
from acrobe.component.arm.coresight.model import ComponentIds
from acrobe.component.arm.coresight.scs import Scs
from acrobe.node import Node
from acrobe.target.arm.cortex_m import (
    CORTEX_M_REGISTERS, CortexMCore, CortexMDebuggable,
)
from acrobe.target.debuggable import CoreState, HaltCause


class MockBus:
    """In-memory bus simulating a Mem-AP. Tracks every read/write
    for assertions and exposes read32/write32 + mem_read/mem_write."""

    def __init__(self):
        self.memory: dict[int, int] = {}
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int]] = []
        # Optional handlers for special addresses (e.g. side effects
        # of writing to DHCSR / DCRSR).
        self.write_hooks: dict[int, callable] = {}

    def __make_future(self, value):
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        fut.set_result(value)
        return fut

    def read32(self, addr):
        value = self.memory.get(addr, 0)
        self.reads.append((addr, value))
        return self.__make_future(value)

    def write32(self, addr, data):
        self.writes.append((addr, data))
        if addr in self.write_hooks:
            self.write_hooks[addr](data)
        else:
            self.memory[addr] = data
        return self.__make_future(None)

    def mem_read(self, addr, size):
        data = bytes(
            self.memory.get(addr + i, 0) & 0xff for i in range(size))
        return self.__make_future(data)

    def mem_write(self, addr, data):
        for i, b in enumerate(data):
            self.memory[addr + i] = b
        return self.__make_future(None)


def make_scs(bus=None, base=0xE000E000):
    bus = bus or MockBus()
    scs = Scs(bus, base, ComponentIds.empty())
    return scs, bus


def make_fpb(bus=None, base=0xE0002000, *, code_count=6, lit_count=0):
    bus = bus or MockBus()
    bus.memory[base + Fpb.CTRL_OFFSET] = (
        ((code_count & 0xF) << 4)
        | (((code_count >> 4) & 0x7) << 12)
        | ((lit_count & 0xF) << 8)
    )
    fpb = Fpb(bus, base, ComponentIds.empty())
    return fpb, bus


# -- SCS ---------------------------------------------------------------

class TestScsRegisters:
    @pytest.mark.asyncio
    async def test_read_dhcsr(self):
        scs, bus = make_scs()
        bus.memory[scs.base + scs.DHCSR_OFFSET] = 0x00020000  # S_HALT
        assert await scs.read_dhcsr() == 0x00020000

    @pytest.mark.asyncio
    async def test_dhcsr_modify_preserves_other_bits(self):
        scs, bus = make_scs()
        bus.memory[scs.base + scs.DHCSR_OFFSET] = 0x0000001A  # arbitrary set bits
        await scs.dhcsr_modify(set_bits=scs.DHCSR_C_HALT,
                               clear_bits=scs.DHCSR_C_STEP)
        # First read, then one write.
        writes = [(a, v) for a, v in bus.writes
                  if a == scs.base + scs.DHCSR_OFFSET]
        assert len(writes) == 1
        addr, val = writes[0]
        assert val & 0xFFFF0000 == scs.DHCSR_KEY
        low = val & 0xFFFF
        # C_HALT set, C_STEP cleared, other low bits preserved.
        assert low & scs.DHCSR_C_HALT
        assert not (low & scs.DHCSR_C_STEP)
        # Other low bits of original (0x18) survive.
        assert low & 0x18

    @pytest.mark.asyncio
    async def test_demcr_modify(self):
        scs, bus = make_scs()
        bus.memory[scs.base + scs.DEMCR_OFFSET] = scs.DEMCR_TRCENA
        await scs.demcr_modify(set_bits=scs.DEMCR_VC_CORERESET, clear_bits=0)
        writes = [(a, v) for a, v in bus.writes
                  if a == scs.base + scs.DEMCR_OFFSET]
        addr, val = writes[-1]
        assert val & scs.DEMCR_TRCENA
        assert val & scs.DEMCR_VC_CORERESET


class TestScsRunControl:
    @pytest.mark.asyncio
    async def test_cpu_halt_sets_halt_bit(self):
        scs, bus = make_scs()
        await scs.cpu_halt()
        # Last DHCSR write must include C_HALT + C_DEBUGEN with key.
        writes = [(a, v) for a, v in bus.writes
                  if a == scs.base + scs.DHCSR_OFFSET]
        assert writes, "no DHCSR write issued"
        _, val = writes[-1]
        assert val & 0xFFFF0000 == scs.DHCSR_KEY
        assert val & scs.DHCSR_C_HALT
        assert val & scs.DHCSR_C_DEBUGEN
        assert not (val & scs.DHCSR_C_STEP)

    @pytest.mark.asyncio
    async def test_cpu_step_sequence(self):
        scs, bus = make_scs()
        await scs.cpu_step()
        dhcsr_writes = [v for a, v in bus.writes
                        if a == scs.base + scs.DHCSR_OFFSET]
        # Two DHCSR writes: arm with HALT, then STEP.
        assert len(dhcsr_writes) == 2
        assert dhcsr_writes[0] & scs.DHCSR_C_HALT
        assert dhcsr_writes[1] & scs.DHCSR_C_STEP
        assert dhcsr_writes[1] & scs.DHCSR_C_MASKINTS
        # DFSR was cleared.
        assert any(a == scs.base + scs.DFSR_OFFSET and v == scs.DFSR_CLEAR
                   for a, v in bus.writes)

    @pytest.mark.asyncio
    async def test_cpu_resume_with_interrupts(self):
        scs, bus = make_scs()
        await scs.cpu_resume(allow_interrupts=True)
        dhcsr_writes = [v for a, v in bus.writes
                        if a == scs.base + scs.DHCSR_OFFSET]
        # Last write: just DEBUGEN, no HALT, no MASKINTS.
        last = dhcsr_writes[-1]
        assert last & scs.DHCSR_C_DEBUGEN
        assert not (last & scs.DHCSR_C_HALT)
        assert not (last & scs.DHCSR_C_MASKINTS)


class TestScsRegisterIo:
    @pytest.mark.asyncio
    async def test_cpu_regs_get_writes_selector_reads_data(self):
        scs, bus = make_scs()
        # Stage DCRDR with sequential values; the test verifies the
        # DCRSR write/DCRDR read sequence rather than realistic CPU
        # behaviour. Increment DCRDR on every selector write.
        counter = [0]
        results = [0x11111111, 0x22222222, 0x33333333]

        def on_dcrsr_write(_value):
            bus.memory[scs.base + scs.DCRDR_OFFSET] = results[counter[0]]
            counter[0] += 1

        bus.write_hooks[scs.base + scs.DCRSR_OFFSET] = on_dcrsr_write
        values = await scs.cpu_regs_get([0, 1, 15])
        assert values == results
        # Each register triggered one DCRSR write + one DCRDR read.
        dcrsr_writes = [v for a, v in bus.writes
                        if a == scs.base + scs.DCRSR_OFFSET]
        assert dcrsr_writes == [0, 1, 15]

    @pytest.mark.asyncio
    async def test_cpu_regs_set_writes_data_then_selector_with_write_bit(self):
        scs, bus = make_scs()
        await scs.cpu_regs_set([(0, 0xDEADBEEF), (15, 0x08000001)])
        # Walk through bus.writes in submission order.
        scs_writes = [(a, v) for a, v in bus.writes
                      if a in (scs.base + scs.DCRDR_OFFSET,
                               scs.base + scs.DCRSR_OFFSET)]
        assert scs_writes == [
            (scs.base + scs.DCRDR_OFFSET, 0xDEADBEEF),
            (scs.base + scs.DCRSR_OFFSET, 0 | scs.DCRSR_WRITE),
            (scs.base + scs.DCRDR_OFFSET, 0x08000001),
            (scs.base + scs.DCRSR_OFFSET, 15 | scs.DCRSR_WRITE),
        ]


class TestScsReset:
    @pytest.mark.asyncio
    async def test_cpu_reset_writes_aircr_and_polls(self):
        scs, bus = make_scs()
        # DHCSR S_RESET_ST is clear by default → poll exits immediately.
        await scs.cpu_reset(poll_interval=0)
        # AIRCR write present with key + SYSRESETREQ.
        aircr = [v for a, v in bus.writes
                 if a == scs.base + scs.AIRCR_OFFSET]
        assert aircr
        assert aircr[-1] & 0xFFFF0000 == scs.AIRCR_KEY
        assert aircr[-1] & scs.AIRCR_SYSRESETREQ

    @pytest.mark.asyncio
    async def test_cpu_reset_waits_for_s_reset_st(self):
        scs, bus = make_scs()
        # Pretend reset is active for two polls then clears.
        polls = [0]

        original_read32 = bus.read32

        def read32(addr):
            if addr == scs.base + scs.DHCSR_OFFSET:
                value = scs.DHCSR_S_RESET_ST if polls[0] < 2 else 0
                polls[0] += 1
                loop = asyncio.get_event_loop()
                fut = loop.create_future()
                fut.set_result(value)
                return fut
            return original_read32(addr)

        bus.read32 = read32
        await scs.cpu_reset(poll_interval=0)
        assert polls[0] >= 3  # two RESET_ST=1 polls + one =0

    @pytest.mark.asyncio
    async def test_reset_catch_toggle(self):
        scs, bus = make_scs()
        bus.memory[scs.base + scs.DEMCR_OFFSET] = 0
        await scs.set_reset_catch(True)
        assert bus.memory[scs.base + scs.DEMCR_OFFSET] & scs.DEMCR_VC_CORERESET
        await scs.set_reset_catch(False)
        assert not (bus.memory[scs.base + scs.DEMCR_OFFSET]
                    & scs.DEMCR_VC_CORERESET)


# -- FPB ---------------------------------------------------------------

class TestFpbStart:
    @pytest.mark.asyncio
    async def test_start_decodes_ctrl(self):
        fpb, _ = make_fpb(code_count=6, lit_count=2)
        await fpb.start()
        assert fpb.code_count == 6
        assert fpb.lit_count == 2

    @pytest.mark.asyncio
    async def test_start_decodes_extended_code_count(self):
        """Cortex-M7's FPB has up to 8 code comparators; the high bits
        of NUM_CODE live at CTRL[14:12]."""
        fpb, _ = make_fpb(code_count=8, lit_count=0)
        await fpb.start()
        assert fpb.code_count == 8


class TestFpbBreakpoints:
    @pytest.mark.asyncio
    async def test_enable_writes_key_plus_enable(self):
        fpb, bus = make_fpb()
        await fpb.enable(True)
        writes = [v for a, v in bus.writes if a == fpb.base + fpb.CTRL_OFFSET]
        assert writes[-1] == fpb.CTRL_KEY | fpb.CTRL_ENABLE

    @pytest.mark.asyncio
    async def test_comp_set_lower_halfword(self):
        fpb, bus = make_fpb()
        await fpb.start()
        await fpb.comp_set(0, 0x08000100)  # word-aligned → lower halfword
        addr = fpb.comp_offset(0) + fpb.base
        val = bus.memory[addr]
        assert val & fpb.COMP_ENABLE
        assert val & 0xC0000000 == fpb.COMP_REPLACE_LOWER
        assert val & 0x1FFFFFFC == 0x08000100

    @pytest.mark.asyncio
    async def test_comp_set_upper_halfword(self):
        fpb, bus = make_fpb()
        await fpb.start()
        await fpb.comp_set(0, 0x08000102)  # bit 1 set → upper halfword
        addr = fpb.comp_offset(0) + fpb.base
        val = bus.memory[addr]
        assert val & fpb.COMP_ENABLE
        assert val & 0xC0000000 == fpb.COMP_REPLACE_UPPER

    @pytest.mark.asyncio
    async def test_comp_set_none_clears(self):
        fpb, bus = make_fpb()
        await fpb.start()
        await fpb.comp_set(2, 0x08000100)
        await fpb.comp_set(2, None)
        assert bus.memory[fpb.comp_offset(2) + fpb.base] == 0
        assert 2 not in fpb.allocations

    @pytest.mark.asyncio
    async def test_comp_set_out_of_range(self):
        fpb, _ = make_fpb(code_count=2)
        await fpb.start()
        with pytest.raises(ValueError):
            await fpb.comp_set(5, 0x08000100)

    @pytest.mark.asyncio
    async def test_allocate_and_release(self):
        fpb, _ = make_fpb(code_count=2)
        await fpb.start()
        a = fpb.allocate()
        b = fpb.allocate()
        c = fpb.allocate()
        assert {a, b} == {0, 1}
        assert c is None
        fpb.release(a)
        d = fpb.allocate()
        assert d == a

    @pytest.mark.asyncio
    async def test_comp_clear_zeroes_all(self):
        fpb, bus = make_fpb(code_count=3)
        await fpb.start()
        for i in range(3):
            await fpb.comp_set(i, 0x08000000 + 4 * i)
        await fpb.comp_clear()
        for i in range(3):
            assert bus.memory[fpb.comp_offset(i) + fpb.base] == 0
        assert fpb.allocations == {}


# -- CortexMCore -------------------------------------------------------

class TestCortexMCore:
    def make(self, *, with_fpb=False):
        bus = MockBus()
        scs = Scs(bus, 0xE000E000, ComponentIds.empty())
        fpb = None
        if with_fpb:
            bus.memory[0xE0002000 + Fpb.CTRL_OFFSET] = (6 << 4)
            fpb = Fpb(bus, 0xE0002000, ComponentIds.empty())
        return CortexMCore("core", scs, fpb=fpb), scs, fpb, bus

    @pytest.mark.asyncio
    async def test_state_halt(self):
        core, _, _, bus = self.make()
        bus.memory[core.scs.base + core.scs.DHCSR_OFFSET] = (
            core.scs.DHCSR_S_HALT)
        assert await core.state() == CoreState.HALT

    @pytest.mark.asyncio
    async def test_state_run(self):
        core, _, _, _ = self.make()
        assert await core.state() == CoreState.RUN

    @pytest.mark.asyncio
    async def test_state_lockup_takes_precedence(self):
        core, _, _, bus = self.make()
        bus.memory[core.scs.base + core.scs.DHCSR_OFFSET] = (
            core.scs.DHCSR_S_LOCKUP | core.scs.DHCSR_S_HALT)
        assert await core.state() == CoreState.LOCKUP

    @pytest.mark.asyncio
    async def test_halt_cause_breakpoint(self):
        core, _, _, bus = self.make()
        bus.memory[core.scs.base + core.scs.DFSR_OFFSET] = core.scs.DFSR_BKPT
        assert await core.halt_cause() == HaltCause.BREAKPOINT

    @pytest.mark.asyncio
    async def test_reg_read_returns_dict(self):
        core, scs, _, bus = self.make()
        counter = [0]
        results = [0xAAAA0000, 0xBBBB0000]
        bus.write_hooks[scs.base + scs.DCRSR_OFFSET] = lambda _: (
            bus.memory.__setitem__(
                scs.base + scs.DCRDR_OFFSET, results[counter[0]])
            or counter.__setitem__(0, counter[0] + 1))
        regs = await core.reg_read(["r0", "r1"])
        names = {r.name: v for r, v in regs.items()}
        assert names == {"r0": 0xAAAA0000, "r1": 0xBBBB0000}

    @pytest.mark.asyncio
    async def test_reg_write(self):
        core, scs, _, bus = self.make()
        await core.reg_write({"pc": 0x08000001})
        # DCRDR data + DCRSR with write bit set + register 15.
        pc = core.lookup_register("pc")
        writes = bus.writes
        assert (scs.base + scs.DCRDR_OFFSET, 0x08000001) in writes
        assert (scs.base + scs.DCRSR_OFFSET, pc.number | scs.DCRSR_WRITE) in writes

    @pytest.mark.asyncio
    async def test_reset_cycles_catch(self):
        core, scs, _, bus = self.make()
        await core.reset(stop=True)
        # DEMCR was set then cleared around the reset.
        demcr_writes = [v for a, v in bus.writes
                        if a == scs.base + scs.DEMCR_OFFSET]
        assert len(demcr_writes) == 2
        assert demcr_writes[0] & scs.DEMCR_VC_CORERESET
        assert not (demcr_writes[1] & scs.DEMCR_VC_CORERESET)
        # AIRCR SYSRESETREQ between them.
        aircr_index = next(
            i for i, (a, _) in enumerate(bus.writes)
            if a == scs.base + scs.AIRCR_OFFSET)
        demcr_first_index = next(
            i for i, (a, _) in enumerate(bus.writes)
            if a == scs.base + scs.DEMCR_OFFSET)
        assert demcr_first_index < aircr_index

    @pytest.mark.asyncio
    async def test_breakpoint_add_remove(self):
        core, _, fpb, _ = self.make(with_fpb=True)
        await fpb.start()
        bp = await core.breakpoint_add(0x08000100, kind=2)
        assert bp in await core.breakpoint_list()
        await core.breakpoint_remove(bp)
        assert bp not in await core.breakpoint_list()

    @pytest.mark.asyncio
    async def test_breakpoint_no_fpb_raises(self):
        core, _, _, _ = self.make(with_fpb=False)
        with pytest.raises(NotImplementedError):
            await core.breakpoint_add(0x08000100, kind=2)


# -- CortexMDebuggable -------------------------------------------------

class TestCortexMDebuggable:
    @pytest.mark.asyncio
    async def test_mem_read_delegates(self):
        bus = MockBus()
        bus.memory.update({0x100 + i: 0x10 + i for i in range(4)})
        debug = CortexMDebuggable(bus)
        data = await debug.mem_read(0x100, 4)
        assert data == bytes([0x10, 0x11, 0x12, 0x13])

    @pytest.mark.asyncio
    async def test_mem_write_delegates(self):
        bus = MockBus()
        debug = CortexMDebuggable(bus)
        await debug.mem_write(0x100, b"\xaa\xbb")
        assert bus.memory[0x100] == 0xaa
        assert bus.memory[0x101] == 0xbb

    @pytest.mark.asyncio
    async def test_attach_enables_debug_on_every_scs(self):
        bus = MockBus()
        scs1 = Scs(bus, 0xE000E000, ComponentIds.empty())
        scs2 = Scs(bus, 0xE100E000, ComponentIds.empty())
        debug = CortexMDebuggable(bus)
        debug.child_add(CortexMCore("core0", scs1))
        debug.child_add(CortexMCore("core1", scs2))
        await debug.attach()
        dhcsr_writes = [a for a, _ in bus.writes
                        if a in (scs1.base + scs1.DHCSR_OFFSET,
                                 scs2.base + scs2.DHCSR_OFFSET)]
        assert (scs1.base + scs1.DHCSR_OFFSET) in dhcsr_writes
        assert (scs2.base + scs2.DHCSR_OFFSET) in dhcsr_writes

    @pytest.mark.asyncio
    async def test_from_romtable_builds_cores(self):
        bus = MockBus()
        rom_table = Node("rom-table")
        scs = Scs(bus, 0xE000E000, ComponentIds.empty())
        bus.memory[0xE0002000 + Fpb.CTRL_OFFSET] = (6 << 4)
        fpb = Fpb(bus, 0xE0002000, ComponentIds.empty())
        rom_table._child_attach(scs)
        rom_table._child_attach(fpb)
        debug = CortexMDebuggable.from_romtable(rom_table, bus)
        assert len(debug.cores) == 1
        assert debug.cores[0].scs is scs
        assert debug.cores[0].fpb is fpb

    def test_register_set_complete(self):
        """All CORTEX_M_REGISTERS have distinct numbers and names."""
        numbers = [r.number for r in CORTEX_M_REGISTERS]
        names = [r.name for r in CORTEX_M_REGISTERS]
        assert len(set(numbers)) == len(numbers)
        assert len(set(names)) == len(names)
