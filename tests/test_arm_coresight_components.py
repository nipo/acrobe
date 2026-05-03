"""Smoke tests for the concrete CoreSight component drivers.

Each component class is registered against one or more of:
  * MemoryMappedComponent.devarch_db — keyed on DEVARCH
  * MemoryMappedComponent.db         — keyed on PartId
  * CoresightComponent.db            — keyed on DEVTYPE

These tests verify that classification picks the right class and
that the friendly name lands in the Node name.
"""

import pytest

from acrobe.component.arm.coresight.bus_trace import BusTrace
from acrobe.component.arm.coresight.coproc_trace import CoprocTrace
from acrobe.component.arm.coresight.cti import Cti
from acrobe.component.arm.coresight.dbg import Dbg
from acrobe.component.arm.coresight.dwt import Dwt
from acrobe.component.arm.coresight.etb import Etb
from acrobe.component.arm.coresight.etm import Etm
from acrobe.component.arm.coresight.fpb import Fpb
from acrobe.component.arm.coresight.funnel import Funnel
from acrobe.component.arm.coresight.itm import Itm
from acrobe.component.arm.coresight.model import (
    MemoryMappedComponent, PartId,
)
from acrobe.component.arm.coresight.pmu import Pmu
from acrobe.component.arm.coresight.router import TraceRouter
from acrobe.component.arm.coresight.scs import Scs
from acrobe.component.arm.coresight.stm import Stm
from acrobe.component.arm.coresight.tpiu import Tpiu


class _MemBus:
    """Minimal MemAp-shaped fake; read32(addr) -> Future[int]."""

    def __init__(self):
        self._mem: dict[int, int] = {}

    def install(self, addr: int, value: int):
        self._mem[addr & ~3] = value & 0xffffffff

    def read32(self, addr: int):
        import asyncio
        loop = asyncio.get_running_loop()
        f = loop.create_future()
        f.set_result(self._mem.get(addr & ~3, 0))
        return f


def _arm_partid(part_no: int) -> PartId:
    return PartId(jep106_continuation=4, jep106_id=0x3B, part_no=part_no)


def _install_component(bus, base, *, partid, cidr_class,
                       devarch_archid=0, devarch_present=False,
                       devtype=0):
    """Lay down PIDR/CIDR/DEVARCH/DEVTYPE so ComponentIds.read picks
    up the requested values. ARM-architected (architect=0x23B)."""
    bus.install(base + MemoryMappedComponent.PIDR0,
                partid.part_no & 0xFF)
    bus.install(base + MemoryMappedComponent.PIDR1,
                ((partid.jep106_id & 0xF) << 4)
                | ((partid.part_no >> 8) & 0xF))
    bus.install(base + MemoryMappedComponent.PIDR2,
                (1 << 3) | ((partid.jep106_id >> 4) & 0x7))
    bus.install(base + MemoryMappedComponent.PIDR4,
                partid.jep106_continuation & 0xF)
    bus.install(base + MemoryMappedComponent.CIDR0, 0x0D)
    bus.install(base + MemoryMappedComponent.CIDR1,
                (cidr_class & 0xF) << 4)
    bus.install(base + MemoryMappedComponent.CIDR2, 0x05)
    bus.install(base + MemoryMappedComponent.CIDR3, 0xB1)
    if cidr_class == MemoryMappedComponent.CLASS_CORESIGHT:
        devarch_raw = (
            (0x23B << 21)
            | ((1 if devarch_present else 0) << 20)
            | (devarch_archid & 0xFFFF)
        )
        bus.install(base + MemoryMappedComponent.DEVARCH, devarch_raw)
        bus.install(base + MemoryMappedComponent.DEVTYPE, devtype)


# Each tuple: (label, install kwargs, expected class, expected substring in name).
_DEVTYPE_CASES = [
    ("TPIU",          dict(devtype=0x11), Tpiu,        "Trace Port Interface Unit"),
    ("ETB",           dict(devtype=0x21), Etb,         "Embedded Trace Buffer"),
    ("Funnel",        dict(devtype=0x12), Funnel,      "Trace Funnel"),
    ("ETM-CPU",       dict(devtype=0x13), Etm,         "Embedded Trace Macrocell"),
    ("ITM",           dict(devtype=0x63), Itm,         "Instruction Trace Macrocell"),
    ("CTI",           dict(devtype=0x14), Cti,         "Cross-Trigger Interface"),
    ("Dbg",           dict(devtype=0x15), Dbg,         "Debug Management"),
    ("PMU",           dict(devtype=0x16), Pmu,         "Performance Monitoring Unit"),
    ("CoprocTrace",   dict(devtype=0x33), CoprocTrace, "Coproc Trace"),
    ("BusTrace",      dict(devtype=0x43), BusTrace,    "Bus Trace"),
    ("TraceRouter",   dict(devtype=0x31), TraceRouter, "Basic Trace Router"),
]


@pytest.mark.parametrize("label, kwargs, expected_cls, expected_substr",
                         _DEVTYPE_CASES,
                         ids=lambda x: x if isinstance(x, str) else "")
@pytest.mark.asyncio
async def test_devtype_classification(label, kwargs, expected_cls,
                                      expected_substr):
    bus = _MemBus()
    _install_component(bus, 0xE000_0000,
                       partid=_arm_partid(0x123),
                       cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
                       **kwargs)
    comp = await MemoryMappedComponent.discover(bus, 0xE000_0000)
    assert isinstance(comp, expected_cls)
    assert expected_substr in comp.name


# DEVARCH-keyed registrations (ARM-architected).
_DEVARCH_CASES = [
    ("ETMv4",        0x4A13, Etm, "Embedded Trace Macrocell"),
    ("CTIv2",        0x1A14, Cti, "Cross-Trigger Interface"),
    ("PMUv3",        0x2A16, Pmu, "Performance Monitoring Unit"),
    ("ARMv8 Dbg",    0x6A15, Dbg, "Debug Management"),
    ("STM",          0x0A63, Stm, "System Trace Macrocell"),
    ("ARMv8-M SCS",  0x2A04, Scs, "System Control Space"),
]


@pytest.mark.parametrize("label, archid, expected_cls, expected_substr",
                         _DEVARCH_CASES,
                         ids=lambda x: x if isinstance(x, str) else "")
@pytest.mark.asyncio
async def test_devarch_classification(label, archid, expected_cls,
                                      expected_substr):
    bus = _MemBus()
    _install_component(bus, 0xE000_0000,
                       partid=_arm_partid(0x999),  # unrelated PartId
                       cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
                       devarch_archid=archid,
                       devarch_present=True)
    comp = await MemoryMappedComponent.discover(bus, 0xE000_0000)
    assert isinstance(comp, expected_cls)
    assert expected_substr in comp.name


# PartId-keyed registrations (Cortex-M).
_PARTID_CASES = [
    ("M3-SCS", 0x000, Scs, "System Control Space"),
    ("M3-ITM", 0x001, Itm, "Instruction Trace Macrocell"),
    ("M3-DWT", 0x002, Dwt, "Data Watchpoint and Trace"),
    ("M3-FPB", 0x003, Fpb, "Flash Patch and Breakpoint"),
    ("M0+-DWT", 0x00A,
     # 0x00A isn't actually registered (only 0x002 / DEVARCH); ensure
     # it falls through to the generic class. Skip as a positive case.
     None, None),
    ("M4-SCS", 0x00C, Scs, "System Control Space"),
    ("M7-SCS", 0x00D, Scs, "System Control Space"),
]


@pytest.mark.parametrize("label, part_no, expected_cls, expected_substr",
                         [c for c in _PARTID_CASES if c[2] is not None],
                         ids=lambda x: x if isinstance(x, str) else "")
@pytest.mark.asyncio
async def test_partid_classification(label, part_no, expected_cls,
                                     expected_substr):
    bus = _MemBus()
    # Use class-0xE (Generic IP) so DEVTYPE doesn't apply — we want
    # PartId to be the deciding factor.
    _install_component(bus, 0xE000_E000,
                       partid=_arm_partid(part_no),
                       cidr_class=MemoryMappedComponent.CLASS_GENERIC_IP)
    comp = await MemoryMappedComponent.discover(bus, 0xE000_E000)
    assert isinstance(comp, expected_cls)
    assert expected_substr in comp.name
