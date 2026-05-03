"""Tests for CoreSight component classification:
PIDR/CIDR/DEVARCH parsing, the three Db registries, and discovery
precedence."""

import pytest

from acrobe.component.arm.coresight.model import (
    ComponentIds, CoresightComponent, DevArch,
    MemoryMappedComponent, PartId,
)


# -- A simple bus fixture -------------------------------------------

class FakeBus:
    """Minimal MemAp-shaped object exposing read32(addr) -> Future[int].
    Backed by a sparse word-keyed dict; missing addresses read as 0."""

    def __init__(self):
        self._mem: dict[int, int] = {}
        self._read_log: list[int] = []

    def install_word(self, addr: int, value: int):
        self._mem[addr & ~3] = value & 0xffffffff

    def install_component_ids(self, base: int, *, partid: PartId,
                              cidr_class: int = MemoryMappedComponent.CLASS_CORESIGHT,
                              revision: int = 0, cmod: int = 0,
                              rev_and: int = 0, size_log2: int = 0,
                              devarch_architect: int = 0,
                              devarch_archid: int = 0,
                              devarch_revision: int = 0,
                              devarch_present: bool = False,
                              devtype: int = 0, devid: int = 0):
        """Lay out the 15 ID-bank registers at ``base`` so that
        ComponentIds.read decodes the requested values."""
        # PIDR0..3
        self.install_word(base + MemoryMappedComponent.PIDR0,
                          partid.part_no & 0xFF)
        self.install_word(base + MemoryMappedComponent.PIDR1,
                          ((partid.jep106_id & 0xF) << 4)
                          | ((partid.part_no >> 8) & 0xF))
        self.install_word(base + MemoryMappedComponent.PIDR2,
                          ((revision & 0xF) << 4)
                          | (1 << 3)  # JEDEC = 1
                          | ((partid.jep106_id >> 4) & 0x7))
        self.install_word(base + MemoryMappedComponent.PIDR3,
                          ((rev_and & 0xF) << 4) | (cmod & 0xF))
        self.install_word(base + MemoryMappedComponent.PIDR4,
                          ((size_log2 & 0xF) << 4)
                          | (partid.jep106_continuation & 0xF))
        # PIDR5..7 zero by default — already RAZ via missing entry.

        # CIDR preamble 0xB1_05_<class>0_0D
        self.install_word(base + MemoryMappedComponent.CIDR0, 0x0D)
        self.install_word(base + MemoryMappedComponent.CIDR1,
                          (cidr_class & 0xF) << 4)
        self.install_word(base + MemoryMappedComponent.CIDR2, 0x05)
        self.install_word(base + MemoryMappedComponent.CIDR3, 0xB1)

        if cidr_class == MemoryMappedComponent.CLASS_CORESIGHT:
            devarch_raw = (
                ((devarch_architect & 0x7FF) << 21)
                | ((1 if devarch_present else 0) << 20)
                | ((devarch_revision & 0xF) << 16)
                | (devarch_archid & 0xFFFF)
            )
            self.install_word(base + MemoryMappedComponent.DEVARCH, devarch_raw)
            self.install_word(base + MemoryMappedComponent.DEVTYPE, devtype)
            self.install_word(base + MemoryMappedComponent.DEVID, devid)

    def read32(self, addr: int):
        import asyncio
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._read_log.append(addr)
        future.set_result(self._mem.get(addr & ~3, 0))
        return future


ARM_JEP106 = (4, 0x3B)  # Continuation 4, ID 0x3B → ARCHITECT 0x23B


def _arm_partid(part_no: int) -> PartId:
    return PartId(jep106_continuation=ARM_JEP106[0],
                  jep106_id=ARM_JEP106[1],
                  part_no=part_no)


# -- PartId / DevArch dataclass invariants --------------------------

class TestDataclasses:
    def test_partid_immutable(self):
        from dataclasses import FrozenInstanceError
        pid = _arm_partid(0x4a3)
        with pytest.raises(FrozenInstanceError):
            pid.part_no = 0

    def test_partid_str(self):
        pid = _arm_partid(0x4a3)
        assert "part" in str(pid)
        assert "0x4a3" in str(pid)

    def test_partid_equality(self):
        assert _arm_partid(0x4a3) == _arm_partid(0x4a3)
        assert _arm_partid(0x4a3) != _arm_partid(0x4a4)

    def test_devarch_default_eq(self):
        # Two distinct DevArch values with same architect+archid but
        # different revisions are not equal under default equality.
        a = DevArch(architect=0x23B, archid=0x4A13, revision=1, present=True)
        b = DevArch(architect=0x23B, archid=0x4A13, revision=2, present=True)
        assert a != b

    def test_devarch_db_lookup_masks_revision(self):
        # devarch_db's eq function ignores revision.
        from acrobe.component.arm.coresight.model import _devarch_eq
        a = DevArch(architect=0x23B, archid=0x4A13, revision=1, present=True)
        b = DevArch(architect=0x23B, archid=0x4A13, revision=2, present=True)
        assert _devarch_eq(a, b)

    def test_devarch_db_requires_present(self):
        from acrobe.component.arm.coresight.model import _devarch_eq
        a = DevArch(architect=0x23B, archid=0x4A13, revision=0, present=False)
        b = DevArch(architect=0x23B, archid=0x4A13, revision=0, present=False)
        assert not _devarch_eq(a, b)


# -- Metadata reading and parsing -----------------------------------

class TestMetadataRead:
    @pytest.mark.asyncio
    async def test_arm_class9_with_devarch(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0xE000_0000,
            partid=_arm_partid(0x000),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
            revision=2,
            size_log2=1,
            devarch_architect=0x23B,
            devarch_archid=0x6A15,
            devarch_revision=1,
            devarch_present=True,
            devtype=0x16,
            devid=0xdeadbeef,
        )
        md = await ComponentIds.read(bus, 0xE000_0000)

        assert md.cidr_class == MemoryMappedComponent.CLASS_CORESIGHT
        assert md.partid == _arm_partid(0x000)
        assert md.revision == 2
        assert md.size_log2 == 1
        assert md.devarch is not None
        assert md.devarch.architect == 0x23B
        assert md.devarch.archid == 0x6A15
        assert md.devarch.revision == 1
        assert md.devarch.present is True
        assert md.devtype == 0x16
        assert md.devid == 0xdeadbeef

    @pytest.mark.asyncio
    async def test_class1_rom_table(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0,
            partid=_arm_partid(0xAF7),
            cidr_class=MemoryMappedComponent.CLASS_ROM_TABLE,
        )
        md = await ComponentIds.read(bus, 0)
        assert md.cidr_class == MemoryMappedComponent.CLASS_ROM_TABLE
        # Non-class-9 components: DEVARCH/DEVTYPE/DEVID are not parsed.
        assert md.devarch is None
        assert md.devtype is None
        assert md.devid is None

    @pytest.mark.asyncio
    async def test_invalid_preamble_returns_empty(self):
        bus = FakeBus()
        # Missing component: all reads return 0.
        md = await ComponentIds.read(bus, 0x1000)
        # Empty sentinel: cidr_class is None.
        assert md.cidr_class is None

    @pytest.mark.asyncio
    async def test_size_log2(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0, partid=_arm_partid(0x123),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
            size_log2=2)
        md = await ComponentIds.read(bus, 0)
        assert md.size_log2 == 2  # Component spans 4 × 4 KB = 16 KB.

    @pytest.mark.asyncio
    async def test_pidr_part_number_split(self):
        # PIDR1[3:0] holds part[11:8], PIDR0[7:0] holds part[7:0].
        # Verify 12-bit reconstruction across nibble boundary.
        bus = FakeBus()
        bus.install_component_ids(base=0, partid=_arm_partid(0xFFF))
        md = await ComponentIds.read(bus, 0)
        assert md.partid.part_no == 0xFFF


# -- Discovery and registry precedence ------------------------------

class TestDiscover:
    @pytest.mark.asyncio
    async def test_unknown_component_returns_base_class(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0xE000_0000,
            partid=_arm_partid(0xEAD),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
        )
        comp = await MemoryMappedComponent.discover(bus, 0xE000_0000)
        assert type(comp) is MemoryMappedComponent
        assert comp.partid.part_no == 0xEAD
        assert comp.base == 0xE000_0000

    @pytest.mark.asyncio
    async def test_partid_registry(self):
        partid = _arm_partid(0xAFE)

        @MemoryMappedComponent.db.register(partid)
        class FakePartIdMatch(MemoryMappedComponent):
            pass

        try:
            bus = FakeBus()
            bus.install_component_ids(
                base=0, partid=partid,
                cidr_class=MemoryMappedComponent.CLASS_CORESIGHT)
            comp = await MemoryMappedComponent.discover(bus, 0)
            assert isinstance(comp, FakePartIdMatch)
        finally:
            MemoryMappedComponent.db._registry.pop(partid, None)

    @pytest.mark.asyncio
    async def test_devarch_registry(self):
        archid = 0x4A13
        # We register on a DevArch with REVISION=0, PRESENT=1.
        # Lookup masks REVISION.
        key = DevArch(architect=0x23B, archid=archid,
                      revision=0, present=True)

        @MemoryMappedComponent.devarch_db.register(key)
        class FakeDevArchMatch(MemoryMappedComponent):
            pass

        try:
            bus = FakeBus()
            bus.install_component_ids(
                base=0, partid=_arm_partid(0x999),
                cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
                devarch_architect=0x23B, devarch_archid=archid,
                devarch_revision=3, devarch_present=True)
            comp = await MemoryMappedComponent.discover(bus, 0)
            assert isinstance(comp, FakeDevArchMatch)
        finally:
            MemoryMappedComponent.devarch_db._registry.pop(key, None)

    @pytest.mark.asyncio
    async def test_devtype_registry(self):
        devtype = 0x42

        @CoresightComponent.db.register(devtype)
        class FakeDevTypeMatch(MemoryMappedComponent):
            pass

        try:
            bus = FakeBus()
            bus.install_component_ids(
                base=0, partid=_arm_partid(0x888),
                cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
                devtype=devtype)
            comp = await MemoryMappedComponent.discover(bus, 0)
            assert isinstance(comp, FakeDevTypeMatch)
        finally:
            CoresightComponent.db._registry.pop(devtype, None)

    @pytest.mark.asyncio
    async def test_lookup_precedence_devarch_beats_partid(self):
        partid = _arm_partid(0xABC)
        archid = 0x6A15
        key = DevArch(architect=0x23B, archid=archid,
                      revision=0, present=True)

        @MemoryMappedComponent.devarch_db.register(key)
        class DevArchWinner(MemoryMappedComponent):
            pass

        @MemoryMappedComponent.db.register(partid)
        class PartIdLoser(MemoryMappedComponent):
            pass

        try:
            bus = FakeBus()
            bus.install_component_ids(
                base=0, partid=partid,
                cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
                devarch_architect=0x23B, devarch_archid=archid,
                devarch_present=True)
            comp = await MemoryMappedComponent.discover(bus, 0)
            assert isinstance(comp, DevArchWinner)
        finally:
            MemoryMappedComponent.devarch_db._registry.pop(key, None)
            MemoryMappedComponent.db._registry.pop(partid, None)

    @pytest.mark.asyncio
    async def test_lookup_precedence_partid_beats_devtype(self):
        partid = _arm_partid(0xDEF)
        devtype = 0x55

        @MemoryMappedComponent.db.register(partid)
        class PartIdWinner(MemoryMappedComponent):
            pass

        @CoresightComponent.db.register(devtype)
        class DevTypeLoser(MemoryMappedComponent):
            pass

        try:
            bus = FakeBus()
            bus.install_component_ids(
                base=0, partid=partid,
                cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
                devtype=devtype)
            comp = await MemoryMappedComponent.discover(bus, 0)
            assert isinstance(comp, PartIdWinner)
        finally:
            MemoryMappedComponent.db._registry.pop(partid, None)
            CoresightComponent.db._registry.pop(devtype, None)

    @pytest.mark.asyncio
    async def test_devarch_present_zero_skips_devarch_lookup(self):
        # If DEVARCH.PRESENT=0, the devarch_db lookup must not match.
        archid = 0x6A15
        key = DevArch(architect=0x23B, archid=archid,
                      revision=0, present=True)

        @MemoryMappedComponent.devarch_db.register(key)
        class ShouldNotMatch(MemoryMappedComponent):
            pass

        try:
            bus = FakeBus()
            bus.install_component_ids(
                base=0, partid=_arm_partid(0x111),
                cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
                devarch_architect=0x23B, devarch_archid=archid,
                devarch_present=False)
            comp = await MemoryMappedComponent.discover(bus, 0)
            assert type(comp) is MemoryMappedComponent
        finally:
            MemoryMappedComponent.devarch_db._registry.pop(key, None)

    @pytest.mark.asyncio
    async def test_size_bytes_property(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0, partid=_arm_partid(0x1),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
            size_log2=2)
        comp = await MemoryMappedComponent.discover(bus, 0)
        # 2^2 × 4 KB = 16 KB.
        assert comp.size_bytes == 16 * 1024


# -- Default name heuristic -----------------------------------------

class TestDefaultName:
    @pytest.mark.asyncio
    async def test_class9_with_present_devarch_uses_archid(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0xE000_1000, partid=_arm_partid(0x123),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
            devarch_architect=0x23B, devarch_archid=0x4A13,
            devarch_present=True)
        comp = await MemoryMappedComponent.discover(bus, 0xE000_1000)
        assert "0x4a13" in comp.name
        assert "e0001000" in comp.name

    @pytest.mark.asyncio
    async def test_class9_without_devarch_uses_partid(self):
        bus = FakeBus()
        bus.install_component_ids(
            base=0xE000_2000, partid=_arm_partid(0x002),
            cidr_class=MemoryMappedComponent.CLASS_CORESIGHT,
            devarch_present=False)
        comp = await MemoryMappedComponent.discover(bus, 0xE000_2000)
        assert "0x002" in comp.name
        assert "e0002000" in comp.name

    @pytest.mark.asyncio
    async def test_unknown_uses_unknown_prefix(self):
        bus = FakeBus()
        # No component ID installed.
        comp = await MemoryMappedComponent.discover(bus, 0xDEAD_0000)
        assert comp.name.startswith("unknown")
