"""Tests for Target framework."""

import pytest
from acrobe.target import Target
from acrobe.target.memory import Region, Ram, Flash
from acrobe.loadable import Program, Segment


class MockRam(Ram):
    """RAM with in-memory storage."""

    def __init__(self, name, address, size):
        super().__init__(name, address, size)
        self._data = bytearray(size)

    async def read(self, offset, size):
        return bytes(self._data[offset:offset + size])

    async def write(self, offset, data):
        self._data[offset:offset + len(data)] = data


class MockFlash(Flash):
    """Flash with in-memory storage and erase/write tracking."""

    def __init__(self, name, address, size, page_size=256):
        super().__init__(name, address, size,
                         write_page_size=page_size,
                         erase_page_sizes=[page_size])
        self._data = bytearray(b"\xff" * size)
        self.erase_log = []
        self.write_log = []

    async def read(self, offset, size):
        return bytes(self._data[offset:offset + size])

    async def write(self, offset, data):
        self.write_log.append((offset, bytes(data)))
        self._data[offset:offset + len(data)] = data

    async def erase(self, offset, size):
        self.erase_log.append((offset, size))
        self._data[offset:offset + size] = b"\xff" * size
        if offset == 0 and size == self.size:
            self.is_blank = True


class TestTarget:
    def _make_target(self):
        t = Target("test-target")
        flash = MockFlash("flash", 0x08000000, 0x10000, page_size=256)
        ram = MockRam("sram", 0x20000000, 0x4000)
        t.child_add(flash)
        t.child_add(ram)
        return t, flash, ram

    @pytest.mark.asyncio
    async def test_write_to_flash(self):
        t, flash, _ = self._make_target()

        prog = Program()
        prog.append(Segment(0x08000000, b"\xaa" * 256))

        await t.write(prog)

        assert len(flash.write_log) >= 1
        # Data should be written
        data = await flash.read(0, 256)
        assert data == b"\xaa" * 256

    @pytest.mark.asyncio
    async def test_write_to_ram(self):
        t, _, ram = self._make_target()

        prog = Program()
        prog.append(Segment(0x20000000, b"\xbb" * 16))

        await t.write(prog)

        data = await ram.read(0, 16)
        assert data == b"\xbb" * 16

    @pytest.mark.asyncio
    async def test_write_with_erase(self):
        t, flash, _ = self._make_target()

        prog = Program()
        prog.append(Segment(0x08000000, b"\xcc" * 256))

        await t.write(prog, do_erase=True)

        # Erase should have been called
        assert len(flash.erase_log) >= 1

    @pytest.mark.asyncio
    async def test_write_erases_non_blank_flash(self):
        t, flash, _ = self._make_target()
        flash.is_blank = False

        prog = Program()
        prog.append(Segment(0x08000000, b"\xdd" * 256))

        await t.write(prog)

        # Should erase because not blank
        assert len(flash.erase_log) >= 1

    @pytest.mark.asyncio
    async def test_write_skips_erase_when_blank(self):
        t, flash, _ = self._make_target()
        flash.is_blank = True

        prog = Program()
        prog.append(Segment(0x08000000, b"\xee" * 256))

        await t.write(prog)

        # No erase needed
        assert len(flash.erase_log) == 0

    @pytest.mark.asyncio
    async def test_verify_success(self):
        t, flash, _ = self._make_target()

        prog = Program()
        prog.append(Segment(0x08000000, b"\xaa" * 256))

        await t.write(prog)
        result = await t.verify(prog)
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_failure(self):
        t, flash, _ = self._make_target()

        prog = Program()
        prog.append(Segment(0x08000000, b"\xaa" * 256))

        # Don't write — flash is 0xff, program expects 0xaa
        result = await t.verify(prog)
        assert result is False

    @pytest.mark.asyncio
    async def test_read(self):
        t, flash, ram = self._make_target()

        # Write something to flash
        flash._data[0:4] = b"\x11\x22\x33\x44"

        prog = await t.read()
        assert len(prog.segments) == 2  # flash + ram

        # Flash segment
        flash_seg = [s for s in prog.segments if s.address == 0x08000000][0]
        assert flash_seg.data[:4] == bytearray(b"\x11\x22\x33\x44")

    @pytest.mark.asyncio
    async def test_read_range(self):
        t, flash, ram = self._make_target()
        flash._data[0:4] = b"\xde\xad\xbe\xef"

        prog = await t.read(begin=0x08000000, end=0x08000100)
        assert len(prog.segments) == 1
        assert prog.segments[0].address == 0x08000000
        assert len(prog.segments[0]) == 0x100

    @pytest.mark.asyncio
    async def test_erase_all(self):
        t, flash, _ = self._make_target()
        flash._data[0:4] = b"\xaa\xbb\xcc\xdd"
        flash.is_blank = False

        await t.erase_all()

        assert flash.is_blank
        assert flash._data[0:4] == b"\xff\xff\xff\xff"

    @pytest.mark.asyncio
    async def test_force_blank(self):
        t, flash, _ = self._make_target()
        flash.is_blank = False

        t._force_blank()
        assert flash.is_blank

    @pytest.mark.asyncio
    async def test_write_multi_segment(self):
        """Write program with segments in both flash and RAM."""
        t, flash, ram = self._make_target()
        flash.is_blank = True

        prog = Program()
        prog.append(Segment(0x08000000, b"\x11" * 256))
        prog.append(Segment(0x20000000, b"\x22" * 64))

        await t.write(prog)

        flash_data = await flash.read(0, 256)
        assert flash_data == b"\x11" * 256

        ram_data = await ram.read(0, 64)
        assert ram_data == b"\x22" * 64

    @pytest.mark.asyncio
    async def test_write_segment_outside_regions_ignored(self):
        """Segments not covering any region are silently ignored."""
        t, flash, _ = self._make_target()
        flash.is_blank = True

        prog = Program()
        prog.append(Segment(0x40000000, b"\xff" * 16))

        await t.write(prog)

        # Nothing written
        assert len(flash.write_log) == 0


class TestTargetRegistry:
    def test_register_explorer(self):
        original_len = len(Target._explorers)

        @Target.register(MockRam, precedence=500)
        def discover_mock(component):
            return Target("mock")

        assert len(Target._explorers) == original_len + 1

        # Cleanup
        Target._explorers = [
            e for e in Target._explorers if e.func is not discover_mock
        ]
