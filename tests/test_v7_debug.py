import asyncio

import pytest

from acrobe.component.arm.a32 import A32
from acrobe.component.arm.coresight.model import (
    ComponentIds, MemoryMappedComponent)
from acrobe.component.arm.coresight.v7_debug import (
    Bcr, BreakpointType, Dscr, PrivilegeMatch, V7Debug, V7DebugAbort,
    WatchAccess, Wcr)
from acrobe.part_id import PartId


class FakeA9:
    """Memory-mapped debug registers of a halted core executing ITR
    writes against a tiny register file and memory."""

    def __init__(self):
        self.regs = [0] * 15
        self.memory = {}
        self.dscr = Dscr(halted=True, instrcompl_l=True)
        self.rx = None
        self.tx = None
        self.executed = []
        self.writes = []

    @staticmethod
    def done(value=None):
        f = asyncio.get_running_loop().create_future()
        f.set_result(value)
        return f

    def read32(self, address):
        offset = address & 0xfff
        if offset == V7Debug.DSCR:
            d = Dscr(int(self.dscr))
            d.txfull_l = self.tx is not None
            return self.done(int(d))
        if offset == V7Debug.DTRTX:
            value, self.tx = self.tx, None
            return self.done(value)
        raise AssertionError(f"read {offset:#x}")

    def write32(self, address, data):
        offset = address & 0xfff
        self.writes.append((offset, data))
        if offset == V7Debug.DTRRX:
            self.rx = data
        elif offset == V7Debug.ITR:
            self.execute(data)
        elif offset == V7Debug.DSCR:
            self.dscr = Dscr(data)
        elif offset == V7Debug.DRCR:
            if data & 4:
                self.dscr.sdabort_l = False
                self.dscr.und_l = False
        return self.done()

    def execute(self, instr):
        self.executed.append(instr)
        for rt in range(15):
            if instr == V7Debug.dtr_to_reg(rt):
                self.regs[rt], self.rx = self.rx, None
                return
            if instr == V7Debug.reg_to_dtr(rt):
                self.tx = self.regs[rt]
                return
        if instr == A32.ldc(14, 5, 0, 4, post_index=True):
            self.tx = self.memory.get(self.regs[0], 0)
            self.regs[0] += 4
        elif instr == A32.stc(14, 5, 0, 4, post_index=True):
            self.memory[self.regs[0]], self.rx = self.rx, None
            self.regs[0] += 4
        elif instr == A32.ldrb(1, 0, 1, post_index=True):
            word = self.memory.get(self.regs[0] & ~3, 0)
            self.regs[1] = (word >> (8 * (self.regs[0] & 3))) & 0xff
            self.regs[0] += 1
        else:
            self.dscr.und_l = True


def make_debug(bus):
    ids = ComponentIds(
        cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
        partid=PartId.from_idcode(0xC09477),
        cmod=0, rev_and=0, size_log2=0,
        devarch=None, devtype=0x15, devid=0,
    )
    return V7Debug(bus=bus, base=0x80090000, ids=ids)


def test_registered_by_part_id():
    assert V7Debug in MemoryMappedComponent.db.get(
        PartId.from_idcode(0x00C09477))


async def test_register_roundtrip():
    bus = FakeA9()
    debug = make_debug(bus)
    await debug.reg_set(5, 0xdeadbeef)
    assert bus.regs[5] == 0xdeadbeef
    assert await debug.reg_get(5) == 0xdeadbeef


async def test_words():
    bus = FakeA9()
    debug = make_debug(bus)
    await debug.store_words(0x1000, [1, 2, 3])
    assert bus.memory == {0x1000: 1, 0x1004: 2, 0x1008: 3}
    assert await debug.load_words(0x1004, 2) == [2, 3]


async def test_bytes():
    bus = FakeA9()
    bus.memory[0x2000] = 0x44332211
    debug = make_debug(bus)
    assert await debug.load_units(0x2001, 1, 3) == [0x22, 0x33, 0x44]


async def test_undefined_raises_and_clears():
    bus = FakeA9()
    debug = make_debug(bus)
    with pytest.raises(V7DebugAbort):
        await debug.execute(0xe7f000f0)
    assert not bus.dscr.und_l
    assert (V7Debug.DRCR, 4) in bus.writes


async def test_enter_enables_itr():
    bus = FakeA9()
    debug = make_debug(bus)
    await debug.enter()
    assert bus.dscr.hdbgen and bus.dscr.itren and bus.dscr.adadiscard


async def test_breakpoint_disabled_while_reprogrammed():
    bus = FakeA9()
    debug = make_debug(bus)
    bcr = Bcr(enable=True, privilege=PrivilegeMatch.ANY, byte_select=0b1111,
              type=BreakpointType.ADDRESS_MISMATCH)
    await debug.breakpoint_set(2, 0x1003, bcr)
    assert bus.writes == [(0x148, 0), (0x108, 0x1000), (0x148, 0x004001e7)]


async def test_watchpoint_encoding():
    bus = FakeA9()
    debug = make_debug(bus)
    wcr = Wcr(enable=True, privilege=PrivilegeMatch.ANY,
              access=WatchAccess.STORE, byte_select=0b1111, mask=6)
    await debug.watchpoint_set(3, 0x2040, wcr)
    assert bus.writes == [(0x1cc, 0), (0x18c, 0x2040), (0x1cc, 0x060001f7)]
