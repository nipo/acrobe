"""Tests for BusRam and BusFlash with a mock MemAp."""

import pytest
from crobe_async.component.arm.memory import BusRam, BusFlash
from crobe_async.target.memory import Ram, Flash


class MockMemAp:
    """Minimal MemAp mock with byte-level memory."""

    def __init__(self):
        self._memory = bytearray(0x10000)

    def store(self, addr, data):
        offset = addr - 0x20000000
        self._memory[offset:offset + len(data)] = data

    async def mem_read(self, addr, size):
        offset = addr - 0x20000000
        return bytes(self._memory[offset:offset + size])

    async def mem_write(self, addr, data):
        offset = addr - 0x20000000
        self._memory[offset:offset + len(data)] = data


class TestBusRam:
    def test_is_ram(self):
        ap = MockMemAp()
        r = BusRam("sram", 0x20000000, 0x10000, ap)
        assert isinstance(r, Ram)

    @pytest.mark.asyncio
    async def test_read(self):
        ap = MockMemAp()
        ap.store(0x20000100, b"\xaa\xbb\xcc\xdd")
        r = BusRam("sram", 0x20000000, 0x10000, ap)

        data = await r.read(0x100, 4)
        assert data == b"\xaa\xbb\xcc\xdd"

    @pytest.mark.asyncio
    async def test_write(self):
        ap = MockMemAp()
        r = BusRam("sram", 0x20000000, 0x10000, ap)

        await r.write(0x200, b"\x11\x22\x33\x44")
        data = await r.read(0x200, 4)
        assert data == b"\x11\x22\x33\x44"

    @pytest.mark.asyncio
    async def test_offset_is_relative(self):
        """Offset is relative to region base address."""
        ap = MockMemAp()
        r = BusRam("sram", 0x20000000, 0x10000, ap)

        await r.write(0, b"\xff")
        data = await r.read(0, 1)
        assert data == b"\xff"


class TestBusFlash:
    def test_is_flash(self):
        ap = MockMemAp()
        f = BusFlash("flash", 0x20000000, 0x10000, 256, [4096], ap)
        assert isinstance(f, Flash)

    @pytest.mark.asyncio
    async def test_read(self):
        ap = MockMemAp()
        ap.store(0x20000000, b"\xde\xad\xbe\xef")
        f = BusFlash("flash", 0x20000000, 0x10000, 256, [4096], ap)

        data = await f.read(0, 4)
        assert data == b"\xde\xad\xbe\xef"

    @pytest.mark.asyncio
    async def test_erase_not_implemented(self):
        ap = MockMemAp()
        f = BusFlash("flash", 0x20000000, 0x10000, 256, [4096], ap)
        with pytest.raises(NotImplementedError):
            await f.erase(0, 4096)

    @pytest.mark.asyncio
    async def test_write_not_implemented(self):
        """BusFlash write is inherited from Flash (abstract)."""
        ap = MockMemAp()
        f = BusFlash("flash", 0x20000000, 0x10000, 256, [4096], ap)
        with pytest.raises(NotImplementedError):
            await f.write(0, b"\x00" * 256)
