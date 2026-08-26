"""Tests for `StLinkMemAp` — the MEM-AP whose wire step is ST-Link's
memory command set.

The ST-Link firmware refuses direct CSW writes, so this AP replaces
only the register-op lowering and inherits blob decomposition from the
shared address-space machinery. These tests pin that: blob reads and
writes must work, and must arrive at the firmware as block commands
rather than one command per word.
"""

import pytest

from acrobe.adapter.stlink.mem_ap import StLinkMemAp
from acrobe.node import Node


AP_BASE = 0x00000000
MEM_BASE = 0x20000000


class MockStLinkTransport:
    """Models the AP's memory as a bytearray and records every
    firmware memory command it was handed."""

    def __init__(self, size=0x4000):
        self.storage = bytearray(size)
        self.calls: list[tuple[str, int, int]] = []

    def __offset(self, addr):
        off = addr - MEM_BASE
        assert 0 <= off <= len(self.storage), f"addr 0x{addr:08x} out of range"
        return off

    async def read_mem32(self, ap_num, addr, word_count, csw=0):
        self.calls.append(("read32", addr, word_count * 4))
        off = self.__offset(addr)
        return bytes(self.storage[off:off + word_count * 4])

    async def write_mem32(self, ap_num, addr, data, csw=0):
        assert len(data) % 4 == 0
        self.calls.append(("write32", addr, len(data)))
        off = self.__offset(addr)
        self.storage[off:off + len(data)] = data

    async def read_mem16(self, ap_num, addr, length, csw=0):
        self.calls.append(("read16", addr, length))
        off = self.__offset(addr)
        return bytes(self.storage[off:off + length])

    async def write_mem16(self, ap_num, addr, data, csw=0):
        self.calls.append(("write16", addr, len(data)))
        off = self.__offset(addr)
        self.storage[off:off + len(data)] = data

    async def read_mem8(self, ap_num, addr, length, csw=0):
        self.calls.append(("read8", addr, length))
        off = self.__offset(addr)
        return bytes(self.storage[off:off + length])

    async def write_mem8(self, ap_num, addr, data, csw=0):
        self.calls.append(("write8", addr, len(data)))
        off = self.__offset(addr)
        self.storage[off:off + len(data)] = data


class MockStLinkDp(Node):
    def __init__(self, transport):
        super().__init__("stlink-dp")
        self._transport = transport


def make_ap(size=0x4000):
    transport = MockStLinkTransport(size)
    return StLinkMemAp(MockStLinkDp(transport), base=AP_BASE), transport


class TestBlobAccess:
    @pytest.mark.asyncio
    async def test_aligned_blob_read_is_one_command(self):
        ap, transport = make_ap()
        transport.storage[0:16] = bytes(range(16))
        assert await ap.mem_read(MEM_BASE, 16) == bytes(range(16))
        assert transport.calls == [("read32", MEM_BASE, 16)]

    @pytest.mark.asyncio
    async def test_unaligned_blob_read_is_one_widened_command(self):
        ap, transport = make_ap()
        transport.storage[0:16] = bytes(range(16))
        assert await ap.mem_read(MEM_BASE + 1, 5) == bytes(range(1, 6))
        assert transport.calls == [("read32", MEM_BASE, 8)]

    @pytest.mark.asyncio
    async def test_zero_size_blob_read(self):
        ap, transport = make_ap()
        assert await ap.mem_read(MEM_BASE, 0) == b""
        assert transport.calls == []

    @pytest.mark.asyncio
    async def test_aligned_blob_write_is_one_command(self):
        ap, transport = make_ap()
        payload = bytes(range(0x40, 0x48))
        await ap.mem_write(MEM_BASE, payload)
        assert bytes(transport.storage[0:8]) == payload
        assert transport.calls == [("write32", MEM_BASE, 8)]

    @pytest.mark.asyncio
    async def test_unaligned_blob_write_keeps_byte_granularity(self):
        ap, transport = make_ap()
        transport.storage[0:8] = b"\xff" * 8
        await ap.mem_write(MEM_BASE + 1, b"\xaa\xbb\xcc")
        assert bytes(transport.storage[0:8]) == (
            b"\xff\xaa\xbb\xcc\xff\xff\xff\xff")
        assert transport.calls == [
            ("write8", MEM_BASE + 1, 1), ("write16", MEM_BASE + 2, 2)]

    @pytest.mark.asyncio
    async def test_blob_roundtrip(self):
        ap, transport = make_ap()
        payload = bytes((i * 7) & 0xFF for i in range(300))
        await ap.mem_write(MEM_BASE + 3, payload)
        assert await ap.mem_read(MEM_BASE + 3, 300) == payload

    @pytest.mark.asyncio
    async def test_long_read_splits_at_command_limit(self):
        ap, transport = make_ap(size=0x4000)
        limit = StLinkMemAp.MAX_WORDS_PER_COMMAND * 4
        await ap.mem_read(MEM_BASE, limit + 8)
        assert transport.calls == [
            ("read32", MEM_BASE, limit),
            ("read32", MEM_BASE + limit, 8)]


class TestRegisterAccess:
    @pytest.mark.asyncio
    async def test_read32(self):
        ap, transport = make_ap()
        transport.storage[0:4] = b"\x11\x22\x33\x44"
        assert await ap.read32(MEM_BASE) == 0x44332211
        assert transport.calls == [("read32", MEM_BASE, 4)]

    @pytest.mark.asyncio
    async def test_write32(self):
        ap, transport = make_ap()
        await ap.write32(MEM_BASE, 0x44332211)
        assert bytes(transport.storage[0:4]) == b"\x11\x22\x33\x44"

    @pytest.mark.asyncio
    async def test_read8(self):
        ap, transport = make_ap()
        transport.storage[0:4] = b"\x11\x22\x33\x44"
        assert await ap.read8(MEM_BASE + 2) == 0x33
        assert transport.calls == [("read8", MEM_BASE + 2, 1)]

    @pytest.mark.asyncio
    async def test_read16(self):
        ap, transport = make_ap()
        transport.storage[0:4] = b"\x11\x22\x33\x44"
        assert await ap.read16(MEM_BASE + 2) == 0x4433
        assert transport.calls == [("read16", MEM_BASE + 2, 2)]

    @pytest.mark.asyncio
    async def test_contiguous_word_reads_coalesce(self):
        ap, transport = make_ap()
        transport.storage[0:12] = bytes(range(12))
        futures = [ap.read32(MEM_BASE + i * 4) for i in range(3)]
        values = [await f for f in futures]
        assert values == [0x03020100, 0x07060504, 0x0b0a0908]
        assert transport.calls == [("read32", MEM_BASE, 12)]

    @pytest.mark.asyncio
    async def test_discontiguous_word_reads_do_not_coalesce(self):
        ap, transport = make_ap()
        futures = [ap.read32(MEM_BASE), ap.read32(MEM_BASE + 0x100)]
        for f in futures:
            await f
        assert transport.calls == [
            ("read32", MEM_BASE, 4), ("read32", MEM_BASE + 0x100, 4)]

    @pytest.mark.asyncio
    async def test_backend_failure_reaches_the_caller(self):
        ap, transport = make_ap()

        async def boom(*args, **kwargs):
            raise IOError("USB stall")

        transport.read_mem32 = boom
        with pytest.raises(IOError, match="USB stall"):
            await ap.mem_read(MEM_BASE, 16)
