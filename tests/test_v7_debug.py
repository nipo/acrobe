import asyncio

import pytest

from acrobe.component.arm.a32 import A32
from acrobe.component.arm.coresight.model import (
    ComponentIds, MemoryMappedComponent)
from acrobe.component.arm.coresight.v7_debug import (
    Bcr, BreakpointType, Dscr, ExtDccMode, PrivilegeMatch, V7Debug,
    V7DebugAbort, WatchAccess, Wcr)
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
        self.latched = None
        self.abort_at = None

    @property
    def fast(self):
        return self.dscr.extdccmode == ExtDccMode.FAST

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
            if self.fast:
                self.execute(self.latched)
            return self.done(value)
        raise AssertionError(f"read {offset:#x}")

    def write32(self, address, data):
        offset = address & 0xfff
        self.writes.append((offset, data))
        if offset == V7Debug.DTRRX:
            self.rx = data
            if self.fast:
                self.execute(self.latched)
        elif offset == V7Debug.ITR:
            if self.fast:
                self.latched = data
            else:
                self.execute(data)
        elif offset == V7Debug.DSCR:
            self.dscr = Dscr(data)
        elif offset == V7Debug.DRCR:
            if data & 4:
                self.dscr.sdabort_l = False
                self.dscr.und_l = False
        return self.done()

    def execute(self, instr):
        if self.dscr.sdabort_l:
            return
        self.executed.append(instr)
        if self.abort_at is not None and self.regs[0] == self.abort_at:
            self.dscr.sdabort_l = True
            return
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


class SlowA9(FakeA9):
    """Instructions complete `delay` DSCR reads after their ITR write,
    and ITR / DTR accesses obey the latched DSCR flags of the last
    read like Non-blocking mode does."""

    def __init__(self, delay):
        super().__init__()
        self.delay = delay
        self.pending = None
        self.countdown = 0
        self.gate = Dscr(instrcompl_l=True)
        self.ignored = []

    def read32(self, address):
        offset = address & 0xfff
        if offset == V7Debug.DSCR:
            if self.pending is not None:
                self.countdown -= 1
                if not self.countdown:
                    instr, self.pending = self.pending, None
                    self.execute(instr)
            d = Dscr(int(self.dscr))
            d.instrcompl_l = self.pending is None
            d.txfull_l = self.tx is not None
            d.rxfull_l = self.rx is not None
            self.gate = d
            return self.done(int(d))
        if offset == V7Debug.DTRTX and not self.gate.txfull_l:
            self.ignored.append("DTRTX")
            return self.done(0xbad)
        return super().read32(address)

    def write32(self, address, data):
        offset = address & 0xfff
        if offset == V7Debug.ITR:
            if not self.gate.instrcompl_l:
                self.ignored.append("ITR")
                return self.done()
            self.writes.append((offset, data))
            self.pending, self.countdown = data, self.delay
            return self.done()
        if offset == V7Debug.DTRRX and self.gate.rxfull_l:
            self.ignored.append("DTRRX")
            return self.done()
        return super().write32(address, data)


@pytest.mark.parametrize("delay", [1, 2, 3])
async def test_slow_instructions(delay):
    bus = SlowA9(delay)
    debug = make_debug(bus)
    await debug.reg_set(5, 0xdeadbeef)
    await debug.reg_set(6, 0x12345678)
    assert bus.regs[5:7] == [0xdeadbeef, 0x12345678]
    assert await debug.reg_get(5) == 0xdeadbeef
    assert await debug.reg_get(6) == 0x12345678
    assert "ITR" not in bus.ignored and "DTRRX" not in bus.ignored
    assert ("DTRTX" in bus.ignored) == (delay > 1)


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


async def test_word_runs_use_fast_mode():
    bus = FakeA9()
    debug = make_debug(bus)
    await debug.store_words(0x1000, range(1, 9))
    assert bus.memory == {0x1000 + 4 * i: i + 1 for i in range(8)}
    itr = [d for o, d in bus.writes if o == V7Debug.ITR]
    assert itr.count(A32.stc(14, 5, 0, 4, post_index=True)) == 1
    assert await debug.load_words(0x1000, 8) == list(range(1, 9))
    assert bus.dscr.extdccmode == ExtDccMode.NON_BLOCKING


async def test_abort_in_word_run():
    bus = FakeA9()
    bus.abort_at = 0x1008
    debug = make_debug(bus)
    with pytest.raises(V7DebugAbort):
        await debug.load_words(0x1000, 4)
    assert not bus.dscr.sdabort_l
    assert bus.dscr.extdccmode == ExtDccMode.NON_BLOCKING


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
