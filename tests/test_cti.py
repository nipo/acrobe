import asyncio

from acrobe.component.arm.coresight.cti import Cti
from acrobe.component.arm.coresight.model import (
    ComponentIds, MemoryMappedComponent)
from acrobe.part_id import PartId


class FakeBus:
    def __init__(self, locked=False):
        self.writes = []
        self.lsr = 0b11 if locked else 0b01

    @staticmethod
    def done(value=None):
        f = asyncio.get_running_loop().create_future()
        f.set_result(value)
        return f

    def read32(self, address):
        assert address & 0xfff == Cti.LSR
        return self.done(self.lsr)

    def write32(self, address, data):
        self.writes.append((address & 0xfff, data))
        return self.done()


def make_cti(bus, base=0x80098000, devid=0x00040800):
    ids = ComponentIds(
        cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
        partid=PartId.from_idcode(0x906477), cmod=0, rev_and=0,
        size_log2=0, devarch=None, devtype=0x14, devid=devid)
    return Cti(bus=bus, base=base, ids=ids)


def test_geometry():
    cti = make_cti(None)
    assert cti.geometry().triggers == 8 and cti.geometry().channels == 4


async def test_reset_unroutes_everything():
    bus = FakeBus()
    await make_cti(bus).reset()
    assert bus.writes[0] == (Cti.CONTROL, 0)
    assert (Cti.INEN + 4 * 7, 0) in bus.writes
    assert (Cti.OUTEN + 4 * 7, 0) in bus.writes
    assert bus.writes[-1] == (Cti.INTACK, 0xff)


async def test_unlock_only_when_locked():
    bus = FakeBus()
    await make_cti(bus).unlock()
    assert bus.writes == []
    bus = FakeBus(locked=True)
    await make_cti(bus).unlock()
    assert bus.writes == [(Cti.LAR, Cti.LAR_KEY)]


async def test_channel_registers():
    bus = FakeBus()
    cti = make_cti(bus)
    await cti.channel_set(1)
    await cti.channel_clear(1)
    await cti.pulse(2)
    await cti.trigger_out_route(7, 2)
    assert bus.writes == [(Cti.APPSET, 1), (Cti.APPCLEAR, 1),
                          (Cti.APPPULSE, 2), (Cti.OUTEN + 28, 2)]
