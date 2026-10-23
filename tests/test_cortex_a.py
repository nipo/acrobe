import pytest

from acrobe.component.arm.a32 import A32
from acrobe.component.arm.a32_state import Mode, Psr
from acrobe.component.arm.coresight.v7_debug import (
    Dscr, MethodOfEntry, V7DebugAbort,
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

    async def dscr(self):
        return Dscr(halted=self.halted, moe=MethodOfEntry.HALT_REQUEST)

    async def enter(self):
        return await self.dscr()

    async def halt(self):
        self.halted = True

    async def restart(self):
        self.halted = False

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
