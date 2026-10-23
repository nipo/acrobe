import pytest

from acrobe.component.arm.a32 import A32
from acrobe.component.arm.a32_state import Mode, Psr
from acrobe.component.arm.coresight.v7_debug import (
    BreakpointType, Dscr, MethodOfEntry, V7DebugAbort, WatchAccess,
)
from acrobe.target.arm.cortex_a import CortexACore, CortexAMemoryAbort
from acrobe.target.debuggable import CoreState, HaltCause


class FakeV7Debug:
    """Halted ARMv7 core at the V7Debug primitive level."""

    def __init__(self, pc=0x1000, cpsr=None):
        self.regs = [0x100 + r for r in range(15)]
        self.pc = pc
        self.cpsr = int(Psr(mode=Mode.SYS)) if cpsr is None else cpsr
        self.halted = False
        self.memory = {}
        self.executed = []
        self.abort_at = None
        self.sctlr = 0x00c5187d
        self.moe = MethodOfEntry.HALT_REQUEST
        self.breakpoints = {}
        self.watchpoints = {}
        self.intdis = False
        self.intdis_on_restart = None

    async def dscr(self):
        return Dscr(halted=self.halted, moe=self.moe, intdis=self.intdis)

    async def dscr_update(self, **fields):
        self.intdis = fields.get("intdis", self.intdis)

    async def breakpoint_count(self):
        return 6

    async def watchpoint_count(self):
        return 4

    async def breakpoint_set(self, index, address, bcr):
        self.breakpoints[index] = (address, bcr)

    async def breakpoint_clear(self, index):
        self.breakpoints.pop(index, None)

    async def watchpoint_set(self, index, address, wcr):
        self.watchpoints[index] = (address, wcr)

    async def watchpoint_clear(self, index):
        self.watchpoints.pop(index, None)

    async def enter(self):
        return await self.dscr()

    async def halt(self):
        self.halted = True

    async def restart(self):
        self.halted = False
        self.intdis_on_restart = self.intdis
        types = {bcr.type: address for address, bcr in self.breakpoints.values()}
        if types.get(BreakpointType.ADDRESS_MATCH) == self.pc:
            self.halted = True
        elif BreakpointType.ADDRESS_MISMATCH in types:
            self.pc += 2 if Psr(self.cpsr).thumb else 4
            self.halted = True
        if self.halted:
            self.moe = MethodOfEntry.BREAKPOINT

    async def reg_get(self, r):
        return self.regs[r]

    async def reg_set(self, r, v):
        self.regs[r] = v

    async def execute(self, instr):
        self.executed.append(instr)
        if instr == A32.mrs(0):
            self.regs[0] = self.cpsr
        elif instr == A32.msr(0):
            self.cpsr = self.regs[0]
        elif instr == A32.mov(0, A32.PC):
            self.regs[0] = self.pc + (4 if Psr(self.cpsr).thumb else 8)
        elif instr == A32.mov(A32.PC, 0):
            self.pc = self.regs[0] & ~1
        elif instr == A32.mrc(15, 0, 0, 1, 0, 0):
            self.regs[0] = self.sctlr
        elif instr == A32.mcr(15, 0, 0, 1, 0, 0):
            self.sctlr = self.regs[0]
        elif instr == A32.add_imm(0, 0, 32):
            self.regs[0] += 32

    async def execute_get(self, instr, rt=0):
        await self.execute(instr)
        return self.regs[rt]

    async def set_execute(self, instr, value, rt=0):
        self.regs[rt] = value
        await self.execute(instr)

    async def load_units(self, address, width, count):
        self.regs[0] = 0xdead
        if self.abort_at is not None and address <= self.abort_at < address + width * count:
            self.cpsr = int(Psr(mode=Mode.ABT))
            raise V7DebugAbort("abort")
        return [self.memory.get(address + i * width, 0) for i in range(count)]

    async def store_units(self, address, width, values):
        self.regs[0] = 0xdead
        for i, v in enumerate(values):
            self.memory[address + i * width] = v


async def test_halt_saves_context():
    debug = FakeV7Debug(pc=0x2000)
    core = CortexACore("cpu", debug)
    assert await core.state() == CoreState.RUN
    await core.halt()
    assert await core.halt_cause() == HaltCause.DEBUGGER
    regs = await core.reg_read(["r0", "lr", "pc", "cpsr"])
    assert [regs[core.lookup_register(n)] for n in ("r0", "lr", "pc")] \
        == [0x100, 0x10e, 0x2000]
    assert debug.regs[0] == 0x100


async def test_thumb_pc_offset_and_restore():
    debug = FakeV7Debug(pc=0x2002, cpsr=int(Psr(mode=Mode.SVC, T=True)))
    core = CortexACore("cpu", debug)
    await core.halt()
    assert (await core.reg_read([15]))[core.lookup_register(15)] == 0x2002
    await core.reg_write({"r3": 0x55})
    await core.resume()
    assert not debug.halted
    assert debug.pc == 0x2002 and debug.regs[3] == 0x55 and debug.regs[0] == 0x100


async def test_run_at():
    debug = FakeV7Debug()
    core = CortexACore("cpu", debug)
    await core.run_at(0x8000)
    assert debug.pc == 0x8000
    assert Psr(debug.cpsr).mode == Mode.SVC and Psr(debug.cpsr).I


async def test_memory_runs_and_cache_sync():
    debug = FakeV7Debug()
    core = CortexACore("cpu", debug)
    await core.halt()
    await core.mem_write(0x1001, bytes(range(1, 8)))
    assert debug.memory == {0x1001: 1, 0x1002: 0x0302, 0x1004: 0x07060504}
    assert await core.mem_read(0x1002, 2) == bytes([2, 3])
    assert A32.isb() in debug.executed
    assert debug.executed.count(A32.mcr(15, 0, 0, 7, 11, 1)) == 1


async def test_abort_restores_mode():
    debug = FakeV7Debug()
    core = CortexACore("cpu", debug)
    await core.halt()
    debug.abort_at = 0x4000
    with pytest.raises(CortexAMemoryAbort):
        await core.mem_read(0x4000, 4)
    assert Psr(debug.cpsr).mode == Mode.SYS


async def test_caches_disable():
    debug = FakeV7Debug()
    core = CortexACore("cpu", debug)
    await core.caches_disable()
    assert debug.sctlr == 0x00c50078
    assert A32.mcr(15, 0, 0, 8, 7, 0) in debug.executed


async def test_breakpoints_leave_step_unit():
    debug = FakeV7Debug()
    core = CortexACore("cpu", debug)
    await core.halt()
    for i in range(5):
        await core.breakpoint_add(0x1000 + 4 * i, 4)
    with pytest.raises(RuntimeError):
        await core.breakpoint_add(0x2000, 4)
    assert sorted(debug.breakpoints) == [0, 1, 2, 3, 4]
    bp = (await core.breakpoint_list())[1]
    await core.breakpoint_remove(bp)
    thumb = await core.breakpoint_add(0x3002, 2)
    assert thumb == (1, 0x3002, 2)
    address, bcr = debug.breakpoints[1]
    assert address == 0x3002 and bcr.byte_select == 0b1100
    assert bcr.type == BreakpointType.ADDRESS_MATCH and bcr.enable


async def test_step_reports_debugger_halt():
    debug = FakeV7Debug(pc=0x2000)
    core = CortexACore("cpu", debug)
    await core.halt()
    await core.step()
    assert debug.intdis_on_restart and not debug.intdis
    assert debug.breakpoints == {}
    assert (await core.reg_read([15]))[core.lookup_register(15)] == 0x2004
    assert await core.halt_cause() == HaltCause.DEBUGGER


@pytest.mark.parametrize("addr,size,byte_select,mask", [
    (0x1000, 4, 0b1111, 0),
    (0x1001, 1, 0b0010, 0),
    (0x1002, 2, 0b1100, 0),
    (0x1040, 64, 0b1111, 6),
])
def test_watch_span(addr, size, byte_select, mask):
    assert CortexACore.watch_span(addr, size) == (byte_select, mask)


@pytest.mark.parametrize("addr,size", [(0x1003, 2), (0x1004, 8 + 4), (0x1004, 8)])
def test_watch_span_rejected(addr, size):
    with pytest.raises(ValueError):
        CortexACore.watch_span(addr, size)


async def test_watchpoints():
    debug = FakeV7Debug()
    core = CortexACore("cpu", debug)
    await core.halt()
    wp = await core.watchpoint_add(0x2000, 4, 3)
    address, wcr = debug.watchpoints[0]
    assert address == 0x2000 and wcr.access == WatchAccess.LOAD
    assert await core.watchpoint_list() == [wp]
    await core.resume()
    debug.halted = True
    debug.moe = MethodOfEntry.SYNC_WATCHPOINT
    assert await core.watchpoint_hit() == wp
    await core.watchpoint_add(0x3000, 4, 2)
    assert await core.watchpoint_hit() is None
    await core.watchpoint_remove(wp)
    assert list(debug.watchpoints) == [1]
    await core.points_clear()
    assert debug.watchpoints == {}


async def test_resume_steps_over_breakpoint_at_pc():
    debug = FakeV7Debug(pc=0x2000)
    core = CortexACore("cpu", debug)
    await core.halt()
    await core.breakpoint_add(0x2000, 4)
    await core.resume()
    assert debug.pc == 0x2004 and not debug.halted
    assert list(debug.breakpoints) == [0]


async def test_dcache_clean_invalidate_walks_sets_and_ways():
    debug = FakeV7Debug()
    core = CortexACore("cpu", debug)
    issued = []
    execute = debug.execute

    async def record(instr):
        if instr == A32.mcr(15, 0, 0, 7, 14, 2):
            issued.append(debug.regs[0])
        elif instr == A32.mrc(15, 1, 0, 0, 0, 0):
            # 4 ways, 2 sets, 32-byte lines
            debug.regs[0] = 1 << 13 | 3 << 3 | 1
            return
        await execute(instr)
    debug.execute = record
    await core.dcache_clean_invalidate()
    assert sorted(issued) == sorted(
        way << 30 | index << 5 for way in range(4) for index in range(2))


class BankedFakeV7Debug(FakeV7Debug):
    """Adds mode-banked core registers, SPSRs and a VFP that only
    answers with coprocessor access granted and FPEXC.EN set."""

    def __init__(self, mode=Mode.SVC, **kwargs):
        super().__init__(cpsr=int(Psr(mode=mode)), **kwargs)
        self.banks = {}
        self.spsr = {m: 0x1000 + m for m in Mode if m.has_spsr}
        self.d = [0x1111_0000_0000_0000 + i for i in range(32)]
        self.fpscr = 0x03000000
        self.cpacr = 0
        self.fpexc = 0

    def owner(self, r):
        mode = Psr(self.cpsr).mode
        return mode.bank if mode.is_banked(r) else Mode.USR

    def bank(self, mode, r):
        return self.banks.get((mode, r), mode << 8 | r)

    async def reg_get(self, r):
        owner = self.owner(r)
        return self.regs[r] if owner is Mode.USR else self.bank(owner, r)

    async def reg_set(self, r, v):
        owner = self.owner(r)
        if owner is Mode.USR:
            self.regs[r] = v
        else:
            self.banks[owner, r] = v

    def fpu_on(self):
        return (self.cpacr & 0xf << 20) == 0xf << 20 and self.fpexc & 1 << 30

    async def execute(self, instr):
        if instr == A32.msr(0, 1):
            self.cpsr = (self.cpsr & ~0xff) | (self.regs[0] & 0xff)
        elif instr == A32.mrs(0, spsr=True):
            self.regs[0] = self.spsr[Psr(self.cpsr).mode]
        elif instr == A32.msr(0, spsr=True):
            self.spsr[Psr(self.cpsr).mode] = self.regs[0]
        elif instr == A32.mrc(15, 0, 0, 1, 0, 2):
            self.regs[0] = self.cpacr
        elif instr == A32.mcr(15, 0, 0, 1, 0, 2):
            self.cpacr = self.regs[0]
        elif instr == A32.vmrs(0, 8):
            assert self.cpacr & 0xf << 20
            self.regs[0] = self.fpexc
        elif instr == A32.vmsr(0, 8):
            self.fpexc = self.regs[0]
        elif instr == A32.vmrs(0):
            assert self.fpu_on()
            self.regs[0] = self.fpscr
        elif instr == A32.vmsr(0):
            assert self.fpu_on()
            self.fpscr = self.regs[0]
        elif any(instr == A32.vmov_from_d(0, 1, i) for i in range(32)):
            assert self.fpu_on()
            i = next(i for i in range(32) if instr == A32.vmov_from_d(0, 1, i))
            self.regs[0], self.regs[1] = self.d[i] & 0xffffffff, self.d[i] >> 32
        elif any(instr == A32.vmov_to_d(i, 0, 1) for i in range(32)):
            assert self.fpu_on()
            i = next(i for i in range(32) if instr == A32.vmov_to_d(i, 0, 1))
            self.d[i] = self.regs[0] | self.regs[1] << 32
        else:
            await super().execute(instr)


async def read(core, *names):
    values = await core.reg_read(list(names))
    return [values[core.lookup_register(n)] for n in names]


async def test_banked_read_switches_mode_and_back():
    debug = BankedFakeV7Debug(mode=Mode.SVC)
    core = CortexACore("cpu", debug)
    await core.halt()
    sp_svc, = await read(core, "sp")
    assert await read(core, "r13_svc", "r13_irq", "spsr_irq", "r8_usr") == \
        [sp_svc, Mode.IRQ << 8 | 13, debug.spsr[Mode.IRQ], 0x108]
    assert Psr(debug.cpsr).mode is Mode.SVC


async def test_fiq_mode_user_registers():
    debug = BankedFakeV7Debug(mode=Mode.FIQ)
    core = CortexACore("cpu", debug)
    await core.halt()
    r8, = await read(core, "r8")
    assert r8 == Mode.FIQ << 8 | 8
    assert await read(core, "r8_fiq", "r8_usr") == [r8, 0x108]


async def test_banked_write_lands_on_resume():
    debug = BankedFakeV7Debug(mode=Mode.SVC)
    core = CortexACore("cpu", debug)
    await core.halt()
    await core.reg_write({"r14_abt": 0xabcd, "spsr_irq": 0x1d3, "sp": 0x8000})
    assert debug.bank(Mode.ABT, 14) != 0xabcd
    assert await read(core, "r14_abt", "r13_svc") == [0xabcd, 0x8000]
    await core.resume()
    assert debug.bank(Mode.ABT, 14) == 0xabcd
    assert debug.spsr[Mode.IRQ] == 0x1d3
    assert debug.bank(Mode.SVC, 13) == 0x8000
    assert Psr(debug.cpsr).mode is Mode.SVC


async def test_vfp_access_restores_fpu_state():
    debug = BankedFakeV7Debug()
    core = CortexACore("cpu", debug, vfp_registers=32, neon=True)
    await core.halt()
    assert await read(core, "d31", "fpscr") == [debug.d[31], 0x03000000]
    assert debug.cpacr == 0 and debug.fpexc == 0
    await core.reg_write({"d3": 0x4000000000000000, "fpscr": 0})
    await core.resume()
    assert debug.d[3] == 0x4000000000000000 and debug.fpscr == 0
    assert debug.cpacr == 0 and debug.fpexc == 0


def test_register_catalog():
    core = CortexACore("cpu", FakeV7Debug(), vfp_registers=32, neon=True)
    names = [r.name for r in core.registers]
    assert names[17:19] == ["d0", "d1"] and "d31" in names
    assert core.lookup_register("fpscr").number == 58
    assert core.lookup_register("r8_usr").number == 59
    assert "spsr_mon" in names and "spsr_usr" not in names
    assert all(not r.gdb_in_g_packet for r in core.registers[17:])
    assert core.gdb_empty_features == ("org.gnu.gdb.arm.neon",)
    with pytest.raises(ValueError):
        CortexACore("cpu", FakeV7Debug(), vfp_registers=16, neon=True)


@pytest.mark.parametrize("sctlr,synced", [(0x00c5187d, True), (0x00c50078, False)])
async def test_write_syncs_caches_only_when_enabled(sctlr, synced):
    debug = FakeV7Debug()
    debug.sctlr = sctlr
    core = CortexACore("cpu", debug)
    await core.halt()
    await core.mem_write(0x2000, bytes(8))
    assert (A32.mcr(15, 0, 0, 7, 5, 1) in debug.executed) == synced
