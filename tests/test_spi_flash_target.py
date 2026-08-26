"""Tests for SPI flash target and discovery."""

import pytest
from acrobe.component.spi_flash import SpiFlash
from acrobe.memory_map import MemoryMap
from acrobe.node import Node
from acrobe.protocol.memory import ReadBlob, WriteBlob
from acrobe.target import Loadable, Target, TargetDiscovery
from acrobe.target.region import Flash
from acrobe.target.spi_flash import SpiFlashBank, SpiFlashTarget


def _mm(*chunks):
    m = MemoryMap()
    for addr, data in chunks:
        m.append(addr, data)
    return m


class FakeSpiTarget:
    """Minimal SPI target stub for SpiFlash."""

    def __init__(self):
        self.transactions = []

    async def transaction(self, *shifts):
        self.transactions.append(shifts)
        for s in shifts:
            if s.read_miso:
                s.miso = bytes(s.byte_count)


class FakeSpiFlash(SpiFlash):
    """SpiFlash whose address-space lowering hits a bytearray."""

    def __init__(self, size=0x10000, page_size=256, sector_info=None):
        spi_target = FakeSpiTarget()
        super().__init__(spi_target)
        self.total_size = size
        self.page_size = page_size
        self.sector_info = sector_info or [(4096, b"\x20")]
        self.storage = bytearray(b"\xff" * size)
        self.erase_log = []
        self.program_log = []
        self.chip_erased = False

    async def run_ops(self, batch):
        for op, future in batch:
            if isinstance(op, ReadBlob):
                future.set_result(
                    bytes(self.storage[op.addr:op.addr + op.size]))
            elif isinstance(op, WriteBlob):
                self.program_log.append((op.addr, op.data))
                self.storage[op.addr:op.addr + len(op.data)] = op.data
                future.set_result(None)
            else:
                future.set_exception(
                    TypeError(f"unexpected op {op!r}"))

    async def erase(self, addr, size):
        self.erase_log.append((addr, size))
        self.storage[addr:addr + size] = b"\xff" * size

    async def erase_chip(self):
        self.chip_erased = True
        self.storage[:] = b"\xff" * len(self.storage)


class TestSpiFlashBank:
    def make_bank(self, **kw):
        flash = FakeSpiFlash(**kw)
        return SpiFlashBank(flash), flash

    def test_geometry(self):
        bank, _ = self.make_bank(size=0x100000, page_size=256,
                                 sector_info=[(4096, b"\x20"),
                                              (65536, b"\xd8")])
        assert bank.address == 0
        assert bank.size == 0x100000
        assert bank.write_page_size == 256
        assert bank.erase_page_sizes == [4096, 65536]

    @pytest.mark.asyncio
    async def test_read(self):
        bank, flash = self.make_bank()
        flash.storage[0:4] = b"\xde\xad\xbe\xef"
        assert await bank.read(0, 4) == b"\xde\xad\xbe\xef"

    @pytest.mark.asyncio
    async def test_write_delegates_to_program(self):
        bank, flash = self.make_bank()
        await bank.write(0x100, b"\xaa\xbb")
        assert flash.program_log == [(0x100, b"\xaa\xbb")]

    @pytest.mark.asyncio
    async def test_erase_partial(self):
        bank, flash = self.make_bank()
        await bank.erase(0x1000, 0x1000)
        assert flash.erase_log == [(0x1000, 0x1000)]
        assert not flash.chip_erased

    @pytest.mark.asyncio
    async def test_erase_full_chip(self):
        bank, flash = self.make_bank(size=0x10000)
        await bank.erase(0, 0x10000)
        assert flash.chip_erased
        assert bank.is_blank


class TestSpiFlashTarget:
    @staticmethod
    def loadable_of(target):
        return target.children_of_class(Loadable)[0]

    def make_target(self, **kw):
        flash = FakeSpiFlash(**kw)
        return SpiFlashTarget(flash), flash

    def test_name(self):
        target, _ = self.make_target()
        assert "SPI flash" in target.name

    def test_has_loadable_with_bank(self):
        target, _ = self.make_target()
        loadables = target.children_of_class(Loadable)
        assert len(loadables) == 1
        banks = loadables[0].children_of_class(Flash)
        assert len(banks) == 1
        assert isinstance(banks[0], SpiFlashBank)

    @pytest.mark.asyncio
    async def test_write_programs_flash(self):
        target, flash = self.make_target(size=0x10000, page_size=256)
        await self.loadable_of(target).write(_mm((0, b"\xaa" * 256)))
        assert (await flash.read(0, 256)) == b"\xaa" * 256

    @pytest.mark.asyncio
    async def test_erase_all_uses_chip_erase(self):
        target, flash = self.make_target()
        await self.loadable_of(target).erase_all()
        assert flash.chip_erased

    @pytest.mark.asyncio
    async def test_verify_success(self):
        target, flash = self.make_target()
        flash.storage[0:4] = b"\x11\x22\x33\x44"
        assert await self.loadable_of(target).verify(
            _mm((0, b"\x11\x22\x33\x44"))) is True

    @pytest.mark.asyncio
    async def test_verify_failure(self):
        target, _ = self.make_target()
        assert await self.loadable_of(target).verify(
            _mm((0, b"\x11\x22\x33\x44"))) is False

    @pytest.mark.asyncio
    async def test_read(self):
        target, flash = self.make_target(size=0x1000)
        flash.storage[0:4] = b"\xab\xcd\xef\x01"
        m = await self.loadable_of(target).read(begin=0, end=0x100)
        assert len(m) == 1
        assert m[0][1][:4] == b"\xab\xcd\xef\x01"


class TestSpiFlashRegistration:
    def test_registered_for_spi_flash(self):
        found = any(SpiFlash in e.component_types for e in Target.explorers)
        assert found


class TestDiscovery:
    def make_tree(self):
        root = Node("root")
        flash = FakeSpiFlash(size=0x10000)
        flash.name = "test-flash"
        root.child_add(flash)
        return root, flash

    @pytest.mark.asyncio
    async def test_discover_finds_spi_flash(self):
        root, _ = self.make_tree()
        disc = TargetDiscovery()
        spawned = await disc.run(root)
        assert len(spawned) == 1
        assert isinstance(spawned[0], SpiFlashTarget)
        assert spawned[0].parent is root

    @pytest.mark.asyncio
    async def test_discover_no_duplicate_on_rerun(self):
        root, _ = self.make_tree()
        disc = TargetDiscovery()
        await disc.run(root)
        again = await disc.run(root)
        assert again == []
        assert len(root.children_of_class(Target)) == 1

    @pytest.mark.asyncio
    async def test_discover_empty_tree(self):
        root = Node("empty")
        disc = TargetDiscovery()
        spawned = await disc.run(root)
        assert spawned == []

    @pytest.mark.asyncio
    async def test_discover_two_trees(self):
        """Two component subtrees under one root both yield a Target."""
        root = Node("root")
        for i in range(2):
            f = FakeSpiFlash(size=0x10000)
            f.name = f"flash-{i}"
            root.child_add(f)

        disc = TargetDiscovery()
        spawned = await disc.run(root)
        assert len(spawned) == 2
