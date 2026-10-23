import asyncio

import pytest

from acrobe.component.arm.coresight.cti import Cti
from acrobe.component.arm.coresight.model import (
    ComponentIds, MemoryMappedComponent)
from acrobe.component.arm.coresight.v7_debug import Dscr
from acrobe.part_id import PartId
from acrobe.target.arm.cross_trigger import CrossTrigger


def done(value=None):
    f = asyncio.get_running_loop().create_future()
    f.set_result(value)
    return f


class FakeBus:
    """CTI register file; pulses on the halt and restart channels
    reach every core of the matrix."""

    def __init__(self, matrix, locked=False):
        self.matrix = matrix
        self.writes = []
        self.lsr = 0b11 if locked else 0b01

    def read32(self, address):
        assert address & 0xfff == Cti.LSR
        return done(self.lsr)

    def write32(self, address, data):
        offset = address & 0xfff
        self.writes.append((offset, data))
        self.matrix.log.append(("cti", offset, data))
        if offset == Cti.LAR and data == Cti.LAR_KEY:
            self.lsr = 0b01
        if offset in (Cti.APPPULSE, Cti.APPSET):
            self.matrix.pulse(data)
        return done()


class FakeDebug:
    def __init__(self, core):
        self.core = core

    async def wait_dscr(self, predicate):
        dscr = Dscr(halted=self.core.halted)
        assert predicate(dscr)
        return dscr

    async def restart_prepare(self):
        self.core.log.append((self.core.name, "restart_prepare"))

    async def wait_restarted(self):
        assert not self.core.halted
        return Dscr(restarted=True)


class FakeCore:
    def __init__(self, name, log, halted=False):
        self.name = name
        self.log = log
        self.halted = halted
        self.debug = FakeDebug(self)

    async def halt(self):
        assert self.halted
        self.log.append((self.name, "halt"))

    async def resume_prepare(self):
        assert self.halted
        self.log.append((self.name, "resume_prepare"))


class Matrix:
    def __init__(self, count=2):
        self.log = []
        self.cores = [FakeCore(f"cpu{i}", self.log) for i in range(count)]

    def pulse(self, channels):
        for core in self.cores:
            if channels & 1 << CrossTrigger.CHANNEL_HALT:
                core.halted = True
            if channels & 1 << CrossTrigger.CHANNEL_RESTART:
                core.halted = False


def make_cti(bus, base, devid=0x00040800):
    ids = ComponentIds(
        cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
        partid=PartId.from_idcode(0x906477), cmod=0, rev_and=0,
        size_log2=0, devarch=None, devtype=0x14, devid=devid)
    return Cti(bus=bus, base=base, ids=ids)


def make_group(matrix, locked=False):
    buses = [FakeBus(matrix, locked) for _ in matrix.cores]
    pairs = [(core, make_cti(bus, 0x80098000 + 0x1000 * i))
             for i, (core, bus) in enumerate(zip(matrix.cores, buses))]
    return CrossTrigger(pairs), buses


async def test_setup_routes_and_unlocks():
    matrix = Matrix()
    group, buses = make_group(matrix, locked=True)
    await group.setup()
    assert buses[1].writes == [
        (Cti.LAR, Cti.LAR_KEY),
        (Cti.OUTEN, 1), (Cti.OUTEN + 4 * 7, 2),
        (Cti.GATE, 3), (Cti.CONTROL, 1)]


async def test_halt_all():
    matrix = Matrix()
    group, buses = make_group(matrix)
    await group.halt()
    assert all(core.halted for core in matrix.cores)
    assert buses[0].writes[-3:] == [
        (Cti.APPSET, 1), (Cti.APPCLEAR, 1), (Cti.INTACK, 1)]
    assert not any(o == Cti.APPSET for o, _ in buses[1].writes)
    acks = [i for i, e in enumerate(matrix.log) if e == ("cti", Cti.INTACK, 1)]
    halts = [i for i, e in enumerate(matrix.log) if e[1] == "halt"]
    assert len(acks) == 2 and max(acks) < min(halts)


async def test_resume_all_drops_debug_request_first():
    matrix = Matrix()
    group, buses = make_group(matrix)
    await group.halt()
    del matrix.log[:]
    await group.resume()
    assert not any(core.halted for core in matrix.cores)
    pulse = matrix.log.index(("cti", Cti.APPPULSE, 2))
    assert all(e != ("cti", Cti.INTACK, 1) for e in matrix.log[pulse:])
    assert [e for e in matrix.log[:pulse] if e[0] != "cti"] == [
        ("cpu0", "resume_prepare"), ("cpu0", "restart_prepare"),
        ("cpu1", "resume_prepare"), ("cpu1", "restart_prepare")]


async def test_teardown_only_after_setup():
    matrix = Matrix()
    group, buses = make_group(matrix)
    await group.teardown()
    assert buses[0].writes == []
    await group.setup()
    await group.teardown()
    assert buses[0].writes[-3:] == [
        (Cti.OUTEN, 0), (Cti.OUTEN + 4 * 7, 0), (Cti.INTACK, 1)]


def test_needs_cores():
    with pytest.raises(ValueError):
        CrossTrigger([])
