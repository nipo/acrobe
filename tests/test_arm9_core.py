"""ARM9 run-control: context save/restore, pc correction, memory
access planning, against a behavioural model of Arm9Debug."""

import asyncio

import pytest

from acrobe.component.arm.a32_state import Mode
from acrobe.component.arm.arm9.embedded_ice import (
    DebugControl, DebugStatus, MethodOfEntry,
)
from acrobe.target.arm.arm9 import Arm9Core, Arm9MemoryAbort
from acrobe.target.debuggable import CoreState, HaltCause


def done(value=None):
    f = asyncio.get_running_loop().create_future()
    f.set_result(value)
    return f


class FakeArm9Debug:
    """Executes the Arm9Debug sequences against a register file and
    a byte memory. Debug-speed pc reads return the value an STM
    would capture right after debug entry."""

    def __init__(self, pc=0x1000, cpsr=0x600000d3, abort_at=None):
        self.regs = [0x100 + i for i in range(15)] + [pc]
        self.cpsr = cpsr
        self.memory = {}
        self.halted = False
        self.control = DebugControl()
        self.abort_at = abort_at
        self.resumed_at = None
        self.moe = MethodOfEntry.INTERNAL_DBGRQ

    async def status(self):
        return DebugStatus(dbgack=self.halted, syscomp=True,
                           moe=self.moe if self.halted else MethodOfEntry.NONE)

    async def control_set(self, control):
        self.control = control
        if control.dbgrq:
            self.halted = True

    async def wait_status(self, predicate, timeout=1.0):
        status = await self.status()
        assert predicate(status)
        return status

    def post_regs_read(self, regs):
        return [done(self.regs[r] + (24 if r == 15 else 0))
                for r in sorted(set(regs))]

    def post_regs_write(self, values):
        for r, v in values.items():
            self.regs[r] = v
        return done()

    def post_pc_write(self, pc):
        self.regs[15] = pc
        return done()

    def post_psr_read(self, spsr=False):
        self.regs[0] = 0xdead
        return done(self.cpsr)

    def post_psr_write(self, value, spsr=False):
        self.cpsr = value
        return done()

    def post_branch_resume(self):
        self.resumed_at = self.regs[15]
        self.halted = False
        return done()

    def __access(self, width):
        address = self.regs[0]
        if self.abort_at is not None and address <= self.abort_at < address + width:
            self.cpsr = (self.cpsr & ~0x1f) | Mode.ABT
        self.regs[0] += width
        return address

    async def load_words(self, count):
        return await self.load_units(4, count)

    async def store_words(self, values):
        await self.store_units(4, values)

    async def load_units(self, width, count):
        out = []
        for _ in range(count):
            a = self.__access(width)
            out.append(int.from_bytes(
                bytes(self.memory.get(a + i, 0) for i in range(width)),
                "little"))
        return out

    async def store_units(self, width, values):
        for v in values:
            a = self.__access(width)
            for i, b in enumerate(v.to_bytes(width, "little")):
                self.memory[a + i] = b


def test_access_plan():
    assert Arm9Core.access_plan(0x1001, 9) == [(1, 1), (2, 1), (4, 1), (2, 1)]
    assert Arm9Core.access_plan(0x1000, 4 * 15) == [(4, 14), (4, 1)]
    assert Arm9Core.access_plan(0x1003, 1) == [(1, 1)]


async def test_halt_saves_corrected_context():
    debug = FakeArm9Debug(pc=0x2000)
    core = Arm9Core("core", debug)
    assert await core.state() == CoreState.RUN
    await core.halt()
    assert debug.control.dbgack and debug.control.intdis
    assert not debug.control.dbgrq
    regs = await core.reg_read(["pc", "r0", "cpsr"])
    assert {r.name: v for r, v in regs.items()} == {
        "pc": 0x2000, "r0": 0x100, "cpsr": 0x600000d3}
    assert await core.halt_cause() == HaltCause.DEBUGGER


async def test_resume_restores_clobbered_context():
    debug = FakeArm9Debug(pc=0x2000)
    core = Arm9Core("core", debug)
    await core.halt()
    await core.mem_write(0x8000, b"\x01\x02\x03\x04\x05")
    await core.reg_write({"r3": 0x33})
    await core.resume()
    assert debug.resumed_at == 0x2000
    assert debug.regs[0] == 0x100
    assert debug.regs[1] == 0x101
    assert debug.regs[3] == 0x33
    assert int(debug.control) == 0


async def test_memory_roundtrip_unaligned():
    debug = FakeArm9Debug()
    core = Arm9Core("core", debug)
    await core.halt()
    data = bytes(range(1, 72))
    await core.mem_write(0x4001, data)
    assert bytes(debug.memory[0x4001 + i] for i in range(len(data))) == data
    assert await core.mem_read(0x4001, len(data)) == data


async def test_memory_requires_halt():
    core = Arm9Core("core", FakeArm9Debug())
    with pytest.raises(RuntimeError):
        await core.mem_read(0, 4)


async def test_abort_detected_and_mode_restored():
    debug = FakeArm9Debug(abort_at=0x9008)
    core = Arm9Core("core", debug)
    await core.halt()
    with pytest.raises(Arm9MemoryAbort):
        await core.mem_read(0x9000, 16)
    assert debug.cpsr == 0x600000d3


async def test_run_at_enters_svc_masked():
    debug = FakeArm9Debug(cpsr=0x10)
    core = Arm9Core("core", debug)
    await core.run_at(0x40000000)
    assert debug.resumed_at == 0x40000000
    assert debug.cpsr == 0xd3


async def test_thumb_entry_unsupported():
    debug = FakeArm9Debug()
    core = Arm9Core("core", debug)
    original = debug.status

    async def thumb_status():
        status = await original()
        status.tbit = True
        return status
    debug.status = thumb_status
    with pytest.raises(NotImplementedError):
        await core.halt()
