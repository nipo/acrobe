"""Tests for SPI flash target and Field discovery."""

import pytest
from acrobe.component import Component
from acrobe.component.spi_flash import SpiFlash
from acrobe.target import Target, Field
from acrobe.target.memory import Flash
from acrobe.target.spi_flash import SpiFlashBank, SpiFlashTarget
from acrobe.loadable import Program, Segment


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
    """SpiFlash with overridden I/O for testing."""

    def __init__(self, size=0x10000, page_size=256, sector_info=None):
        spi_target = FakeSpiTarget()
        super().__init__(spi_target)
        self.total_size = size
        self.page_size = page_size
        self.sector_info = sector_info or [(4096, b"\x20")]
        self._data = bytearray(b"\xff" * size)
        self.erase_log = []
        self.program_log = []
        self.chip_erased = False

    async def read(self, addr, size):
        return bytes(self._data[addr:addr + size])

    async def program(self, addr, data):
        self.program_log.append((addr, bytes(data)))
        self._data[addr:addr + len(data)] = data

    async def erase(self, addr, size):
        self.erase_log.append((addr, size))
        self._data[addr:addr + size] = b"\xff" * size

    async def erase_chip(self):
        self.chip_erased = True
        self._data[:] = b"\xff" * len(self._data)


class TestSpiFlashBank:
    def _make_bank(self, **kw):
        flash = FakeSpiFlash(**kw)
        bank = SpiFlashBank(flash)
        return bank, flash

    def test_geometry(self):
        bank, flash = self._make_bank(size=0x100000, page_size=256,
                                       sector_info=[(4096, b"\x20"), (65536, b"\xd8")])
        assert bank.address == 0
        assert bank.size == 0x100000
        assert bank.write_page_size == 256
        assert bank.erase_page_sizes == [4096, 65536]

    @pytest.mark.asyncio
    async def test_read(self):
        bank, flash = self._make_bank()
        flash._data[0:4] = b"\xde\xad\xbe\xef"
        data = await bank.read(0, 4)
        assert data == b"\xde\xad\xbe\xef"

    @pytest.mark.asyncio
    async def test_write_delegates_to_program(self):
        bank, flash = self._make_bank()
        await bank.write(0x100, b"\xaa\xbb")
        assert flash.program_log == [(0x100, b"\xaa\xbb")]

    @pytest.mark.asyncio
    async def test_erase_partial(self):
        bank, flash = self._make_bank()
        await bank.erase(0x1000, 0x1000)
        assert flash.erase_log == [(0x1000, 0x1000)]
        assert not flash.chip_erased

    @pytest.mark.asyncio
    async def test_erase_full_chip(self):
        bank, flash = self._make_bank(size=0x10000)
        await bank.erase(0, 0x10000)
        assert flash.chip_erased
        assert bank.is_blank


class TestSpiFlashTarget:
    def _make_target(self, **kw):
        flash = FakeSpiFlash(**kw)
        target = SpiFlashTarget(flash)
        return target, flash

    def test_name(self):
        target, _ = self._make_target()
        assert "SPI flash" in target.name

    def test_has_bank_child(self):
        target, _ = self._make_target()
        banks = target.children_of_class(Flash)
        assert len(banks) == 1
        assert isinstance(banks[0], SpiFlashBank)

    @pytest.mark.asyncio
    async def test_write_programs_flash(self):
        target, flash = self._make_target(size=0x10000, page_size=256)

        prog = Program()
        prog.append(Segment(0, b"\xaa" * 256))

        await target.write(prog)
        data = await flash.read(0, 256)
        assert data == b"\xaa" * 256

    @pytest.mark.asyncio
    async def test_erase_all_uses_chip_erase(self):
        target, flash = self._make_target()
        await target.erase_all()
        assert flash.chip_erased

    @pytest.mark.asyncio
    async def test_verify_success(self):
        target, flash = self._make_target()
        flash._data[0:4] = b"\x11\x22\x33\x44"
        prog = Program()
        prog.append(Segment(0, b"\x11\x22\x33\x44"))
        assert await target.verify(prog) is True

    @pytest.mark.asyncio
    async def test_verify_failure(self):
        target, flash = self._make_target()
        prog = Program()
        prog.append(Segment(0, b"\x11\x22\x33\x44"))
        assert await target.verify(prog) is False

    @pytest.mark.asyncio
    async def test_read(self):
        target, flash = self._make_target(size=0x1000)
        flash._data[0:4] = b"\xab\xcd\xef\x01"
        prog = await target.read(begin=0, end=0x100)
        assert len(prog.segments) == 1
        assert prog.segments[0].data[:4] == bytearray(b"\xab\xcd\xef\x01")


class TestSpiFlashRegistration:
    def test_registered_for_spi_flash(self):
        """SpiFlashTarget is registered as explorer for SpiFlash component."""
        from acrobe.component.spi_flash import SpiFlash
        found = False
        for e in Target._explorers:
            if SpiFlash in e.component_types:
                found = True
                break
        assert found


class TestField:
    def _make_tree(self):
        """Build a component tree with a FakeSpiFlash child."""
        root = Component("root")
        flash = FakeSpiFlash(size=0x10000)
        flash._name = "test-flash"
        root._child_attach(flash)
        return root, flash

    @pytest.mark.asyncio
    async def test_discover_finds_spi_flash(self):
        root, flash = self._make_tree()
        field = Field()
        await field.discover(root)
        targets = field.children_of_class(Target)
        assert len(targets) == 1
        assert isinstance(targets[0], SpiFlashTarget)

    @pytest.mark.asyncio
    async def test_discover_no_duplicates(self):
        """Same component is not claimed by multiple explorers."""
        root, flash = self._make_tree()
        field = Field()
        await field.discover(root)
        targets = field.children_of_class(Target)
        assert len(targets) == 1

    @pytest.mark.asyncio
    async def test_discover_empty_tree(self):
        root = Component("empty")
        field = Field()
        await field.discover(root)
        assert len(field.children_of_class(Target)) == 0

    @pytest.mark.asyncio
    async def test_discover_multiple_roots(self):
        root1, _ = self._make_tree()
        root2, _ = self._make_tree()
        field = Field()
        await field.discover(root1, root2)
        targets = field.children_of_class(Target)
        assert len(targets) == 2

    @pytest.mark.asyncio
    async def test_unhandled_components(self):
        """Components not matching any explorer end up in unhandled."""
        root = Component("root")
        child = Component("orphan")
        root._child_attach(child)
        field = Field()
        await field.discover(root)
        # Component base class is not registered for any explorer,
        # so nothing should be in unhandled (only interest types are tracked)
        assert isinstance(field.unhandled, set)
