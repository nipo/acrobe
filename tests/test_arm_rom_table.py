"""Tests for the ROM Table walker + PowerGate."""

import pytest

from acrobe.component.arm.coresight.model import (
    ComponentIds, CoresightComponent, DevArch,
    MemoryMappedComponent, PartId,
)
from acrobe.component.arm.coresight.power_gate import (
    FailureKind, PowerGate,
)
from acrobe.component.arm.coresight.rom_table import RomTable
from acrobe.component.arm.dp import DpAccessFailure


# -- Fake bus mirroring tests/test_arm_coresight_model.py ----------

class FakeBus:
    """MemAp-shaped fake exposing read32(addr) -> Future[int]."""

    def __init__(self):
        self._mem: dict[int, int] = {}
        # Set of word-addresses where read32 should raise.
        self._faulting: set[int] = set()

    def install_word(self, addr: int, value: int):
        self._mem[addr & ~3] = value & 0xffffffff

    def fault_at(self, addr: int):
        self._faulting.add(addr & ~3)

    def install_component_ids(self, base: int, *,
                              partid: PartId,
                              cidr_class: int = MemoryMappedComponent.CLASS_CORESIGHT,
                              size_log2: int = 0,
                              devarch_architect: int = 0,
                              devarch_archid: int = 0,
                              devarch_present: bool = False,
                              devtype: int = 0, devid: int = 0):
        self.install_word(base + MemoryMappedComponent.PIDR0,
                          partid.part_no & 0xFF)
        self.install_word(base + MemoryMappedComponent.PIDR1,
                          ((partid.jep106_id & 0xF) << 4)
                          | ((partid.part_no >> 8) & 0xF))
        self.install_word(base + MemoryMappedComponent.PIDR2,
                          (1 << 3) | ((partid.jep106_id >> 4) & 0x7))
        self.install_word(base + MemoryMappedComponent.PIDR3, 0)
        self.install_word(base + MemoryMappedComponent.PIDR4,
                          ((size_log2 & 0xF) << 4)
                          | (partid.jep106_bank & 0xF))
        self.install_word(base + MemoryMappedComponent.CIDR0, 0x0D)
        self.install_word(base + MemoryMappedComponent.CIDR1,
                          (cidr_class & 0xF) << 4)
        self.install_word(base + MemoryMappedComponent.CIDR2, 0x05)
        self.install_word(base + MemoryMappedComponent.CIDR3, 0xB1)
        if cidr_class == MemoryMappedComponent.CLASS_CORESIGHT:
            devarch_raw = (
                ((devarch_architect & 0x7FF) << 21)
                | ((1 if devarch_present else 0) << 20)
                | (devarch_archid & 0xFFFF)
            )
            self.install_word(base + MemoryMappedComponent.DEVARCH, devarch_raw)
            self.install_word(base + MemoryMappedComponent.DEVTYPE, devtype)
            self.install_word(base + MemoryMappedComponent.DEVID, devid)

    def install_rom_entry(self, rom_base: int, idx: int,
                          child_offset: int, *, present: bool = True,
                          entry_size: int = 4):
        """Lay down a ROM entry pointing to ``rom_base + child_offset``."""
        if entry_size == 4:
            field = (child_offset & 0xFFFFF000) | (0b11 if present else 0b00)
            self.install_word(rom_base + idx * 4, field & 0xffffffff)
        else:
            field_lo = (child_offset & 0xFFFFF000) | (0b11 if present else 0b00)
            field_hi = (child_offset >> 32) & 0xffffffff
            self.install_word(rom_base + idx * 8,
                              field_lo & 0xffffffff)
            self.install_word(rom_base + idx * 8 + 4, field_hi)

    def read32(self, addr: int):
        import asyncio
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        word_addr = addr & ~3
        if word_addr in self._faulting:
            future.set_exception(DpAccessFailure(
                f"simulated fault at 0x{addr:x}"))
        else:
            future.set_result(self._mem.get(word_addr, 0))
        return future

    def write32(self, addr: int, data: int):
        import asyncio
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        word_addr = addr & ~3
        if word_addr in self._faulting:
            future.set_exception(DpAccessFailure(
                f"simulated fault at 0x{addr:x}"))
        else:
            self._mem[word_addr] = data & 0xffffffff
            future.set_result(None)
        return future


ARM = (4, 0x3B)


def _arm_partid(part_no: int) -> PartId:
    return PartId(jep106_bank=ARM[0],
                  jep106_id=ARM[1], part_no=part_no)


# -- Class 0x1 ROM dispatch -----------------------------------------

class TestClassOneRomDispatch:
    @pytest.mark.asyncio
    async def test_cidr_class_1_resolves_to_rom_table(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0xE000_0000, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        comp = await MemoryMappedComponent.discover(bus, 0xE000_0000)
        assert isinstance(comp, RomTable)


# -- Class 0x9 ROM dispatch (DEVARCH = 0x0AF7) ----------------------

class TestClassNineRomDispatch:
    @pytest.mark.asyncio
    async def test_archid_0af7_resolves_to_rom_table(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0xE000_0000, partid=_arm_partid(0x999),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
            devarch_architect=0x23B, devarch_archid=0x0AF7,
            devarch_present=True)
        comp = await MemoryMappedComponent.discover(bus, 0xE000_0000)
        assert isinstance(comp, RomTable)


# -- Walking entries ------------------------------------------------

class TestWalk:
    @pytest.mark.asyncio
    async def test_empty_rom_no_children(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0xE000_0000, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        # Entry 0 is 0 → terminator → no children.
        rom = await MemoryMappedComponent.discover(bus, 0xE000_0000)
        await rom.start()
        assert rom.children == []

    @pytest.mark.asyncio
    async def test_single_child_entry(self):
        bus = FakeBus()
        rom_base = 0xE000_0000
        bus.install_component_ids(
            base=rom_base, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        # Entry 0 → child at offset 0x10000 (so child_addr=rom+0x10000).
        bus.install_rom_entry(rom_base, idx=0, child_offset=0x10000)
        # Provide IDs for the child (a generic class-9 component).
        bus.install_component_ids(
            base=rom_base + 0x10000, partid=_arm_partid(0x200),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT)
        # Entry 1 = 0 → terminator.

        rom = await MemoryMappedComponent.discover(bus, rom_base)
        await rom.start()
        assert len(rom.children) == 1
        child = rom.children[0]
        assert isinstance(child, MemoryMappedComponent)
        assert child.base == rom_base + 0x10000
        assert child.partid.part_no == 0x200

    @pytest.mark.asyncio
    async def test_multiple_entries_terminated_by_zero(self):
        bus = FakeBus()
        rom_base = 0xE000_0000
        bus.install_component_ids(
            base=rom_base, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        for i in range(3):
            child_off = 0x10000 * (i + 1)
            bus.install_rom_entry(rom_base, idx=i, child_offset=child_off)
            bus.install_component_ids(
                base=rom_base + child_off,
                partid=_arm_partid(0x200 + i),
                cidr_class=MemoryMappedComponent.CLASS_CORESIGHT)
        # Entry 3 = 0 → terminator.

        rom = await MemoryMappedComponent.discover(bus, rom_base)
        await rom.start()
        assert len(rom.children) == 3
        part_nos = sorted(c.partid.part_no for c in rom.children)
        assert part_nos == [0x200, 0x201, 0x202]

    @pytest.mark.asyncio
    async def test_negative_offset(self):
        # Negative offset entry (two's complement on bits[31:12]).
        bus = FakeBus()
        rom_base = 0x8000_0000
        bus.install_component_ids(
            base=rom_base, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        # Entry 0: offset -0x10000 (two's complement: 0xFFFF_0000 in bits[31:12]).
        bus.install_rom_entry(rom_base, idx=0, child_offset=0xFFFF0000)
        # Child at rom_base - 0x10000 = 0x7FFF_0000.
        bus.install_component_ids(
            base=0x7FFF_0000, partid=_arm_partid(0x300),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT)

        rom = await MemoryMappedComponent.discover(bus, rom_base)
        await rom.start()
        assert len(rom.children) == 1
        assert rom.children[0].base == 0x7FFF_0000


# -- PowerGate failure paths ----------------------------------------

class TestPowerGate:
    @pytest.mark.asyncio
    async def test_unreachable_child_yields_powergate_fault(self):
        bus = FakeBus()
        rom_base = 0xE000_0000
        bus.install_component_ids(
            base=rom_base, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        bus.install_rom_entry(rom_base, idx=0, child_offset=0x10000)
        # Make the child's PIDR0 read fault.
        bus.fault_at(rom_base + 0x10000 + MemoryMappedComponent.PIDR0)

        rom = await MemoryMappedComponent.discover(bus, rom_base)
        await rom.start()
        assert len(rom.children) == 1
        gate = rom.children[0]
        assert isinstance(gate, PowerGate)
        assert gate.failure_kind == FailureKind.FAULT
        assert gate.address == rom_base + 0x10000

    @pytest.mark.asyncio
    async def test_empty_child_yields_powergate_empty(self):
        # Child reads succeed but CIDR preamble doesn't match.
        bus = FakeBus()
        rom_base = 0xE000_0000
        bus.install_component_ids(
            base=rom_base, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        bus.install_rom_entry(rom_base, idx=0, child_offset=0x10000)
        # Don't install component IDs — preamble is all zero.

        rom = await MemoryMappedComponent.discover(bus, rom_base)
        await rom.start()
        assert len(rom.children) == 1
        gate = rom.children[0]
        assert isinstance(gate, PowerGate)
        assert gate.failure_kind == FailureKind.EMPTY

    @pytest.mark.asyncio
    async def test_powergate_retry_succeeds_after_install(self):
        # Walker installs a PowerGate(EMPTY); we then "power up" by
        # installing the component IDs and call retry().
        bus = FakeBus()
        rom_base = 0xE000_0000
        bus.install_component_ids(
            base=rom_base, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        bus.install_rom_entry(rom_base, idx=0, child_offset=0x10000)
        # No child IDs initially.

        rom = await MemoryMappedComponent.discover(bus, rom_base)
        await rom.start()
        gate = rom.children[0]
        assert isinstance(gate, PowerGate)

        # "Power up" the component.
        bus.install_component_ids(
            base=rom_base + 0x10000, partid=_arm_partid(0x400),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT)

        result = await gate.retry()
        assert result is not None
        assert isinstance(result, MemoryMappedComponent)
        assert result.partid.part_no == 0x400
        # Discovered component is parented under the gate.
        assert result in gate.children

    @pytest.mark.asyncio
    async def test_powergate_retry_still_empty_returns_none(self):
        bus = FakeBus()
        rom_base = 0xE000_0000
        bus.install_component_ids(
            base=rom_base, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        bus.install_rom_entry(rom_base, idx=0, child_offset=0x10000)

        rom = await MemoryMappedComponent.discover(bus, rom_base)
        await rom.start()
        gate = rom.children[0]
        # Retry without populating the child — still empty.
        assert await gate.retry() is None
        assert gate.children == []


# -- soc_db override -----------------------------------------------

class TestSocDbOverride:
    @pytest.mark.asyncio
    async def test_soc_override_picks_specific_class(self):
        # A generic class-9 component; the parent ROM has a soc_db
        # entry for (rom_partid, child_addr) → SpecificDriver.
        rom_partid = _arm_partid(0x470)
        child_addr = 0xE000_E000

        class SpecificDriver(MemoryMappedComponent):
            pass

        RomTable.soc_db.register((rom_partid, child_addr))(SpecificDriver)
        try:
            bus = FakeBus()
            bus.install_component_ids(
                base=0xE000_0000, partid=rom_partid,
                cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
            bus.install_rom_entry(0xE000_0000, idx=0,
                                  child_offset=0xE000)  # child at +0xE000 = 0xE000_E000
            bus.install_component_ids(
                base=child_addr, partid=_arm_partid(0x000),
                cidr_class=MemoryMappedComponent.CLASS_CORESIGHT)

            rom = await MemoryMappedComponent.discover(bus, 0xE000_0000)
            await rom.start()
            assert len(rom.children) == 1
            assert isinstance(rom.children[0], SpecificDriver)
        finally:
            RomTable.soc_db._registry.pop((rom_partid, child_addr), None)


# -- Class 0x9 ROM 64-bit entries -----------------------------------

class TestClass9Format1:
    @pytest.mark.asyncio
    async def test_64bit_entries(self):
        # DEVID.FORMAT bit (bit 4) = 1 → 64-bit entries.
        bus = FakeBus()
        rom_base = 0xE000_0000
        bus.install_component_ids(
            base=rom_base, partid=_arm_partid(0xAF7),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
            devarch_architect=0x23B, devarch_archid=0x0AF7,
            devarch_present=True,
            devid=(1 << 4),  # FORMAT bit
        )
        # Entry 0: 64-bit, child at offset 0x20000.
        bus.install_rom_entry(rom_base, idx=0, child_offset=0x20000,
                              entry_size=8)
        bus.install_component_ids(
            base=rom_base + 0x20000, partid=_arm_partid(0x500),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT)

        rom = await MemoryMappedComponent.discover(bus, rom_base)
        assert isinstance(rom, RomTable)
        assert rom.entry_size == 8
        await rom.start()
        assert len(rom.children) == 1
        assert rom.children[0].base == rom_base + 0x20000


# -- Nested ROM Tables ----------------------------------------------

class TestNested:
    @pytest.mark.asyncio
    async def test_rom_pointing_to_rom(self):
        bus = FakeBus()
        outer = 0xE000_0000
        inner = 0xE001_0000
        leaf = 0xE001_1000

        bus.install_component_ids(
            base=outer, partid=_arm_partid(0x100),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        bus.install_rom_entry(outer, idx=0, child_offset=inner - outer)
        # inner ROM
        bus.install_component_ids(
            base=inner, partid=_arm_partid(0x101),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE)
        bus.install_rom_entry(inner, idx=0, child_offset=leaf - inner)
        # leaf component
        bus.install_component_ids(
            base=leaf, partid=_arm_partid(0x600),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT)

        outer_rom = await MemoryMappedComponent.discover(bus, outer)
        await outer_rom.start_tree()  # walks inner too

        assert len(outer_rom.children) == 1
        inner_rom = outer_rom.children[0]
        assert isinstance(inner_rom, RomTable)
        assert len(inner_rom.children) == 1
        leaf_comp = inner_rom.children[0]
        assert leaf_comp.partid.part_no == 0x600
