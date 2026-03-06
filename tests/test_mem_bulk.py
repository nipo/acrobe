"""Tests for MemAp.mem_read() and MemAp.mem_write() bulk access."""

import asyncio
import struct
import pytest
from acrobe.component.arm.dp import SwDp
from acrobe.component.arm.ap import MemAp, MemRead, MemWrite
from acrobe.protocol.swd import Read as SwdRead, Write as SwdWrite
from acrobe.engine import Batcher


class MemoryMockSwdInterface(Batcher):
    """Simplified SWD mock that tracks TAR/CSW and reads from a memory dict.

    Models SWD read pipelining: AP reads return the PREVIOUS read's data.
    The final read's data is captured via RDBUFF.
    """

    def __init__(self):
        super().__init__()
        self._memory = {}
        self._tar = 0
        self._csw = 0
        self._pending_read = 0  # data from previous AP read, returned next
        self.write_log = []  # (addr, data, size) of all DRW writes

    def store_bytes(self, addr, data):
        for i, b in enumerate(data):
            word_addr = (addr + i) & ~3
            byte_pos = (addr + i) & 3
            if word_addr not in self._memory:
                self._memory[word_addr] = 0
            self._memory[word_addr] = (
                (self._memory[word_addr] & ~(0xff << (byte_pos * 8)))
                | (b << (byte_pos * 8))
            )

    def read_bytes(self, addr, size):
        result = bytearray()
        for i in range(size):
            word_addr = (addr + i) & ~3
            byte_pos = (addr + i) & 3
            word = self._memory.get(word_addr, 0)
            result.append((word >> (byte_pos * 8)) & 0xff)
        return bytes(result)

    def _write_memory(self, addr, data, size):
        """Write data into simulated memory at addr with given access size."""
        shift = (addr & 3) * 8
        val = (data >> shift) & ((1 << (size * 8)) - 1)
        self.write_log.append((addr, val, size))
        word_addr = addr & ~3
        if word_addr not in self._memory:
            self._memory[word_addr] = 0
        mask = ((1 << (size * 8)) - 1) << (shift)
        self._memory[word_addr] = (
            (self._memory[word_addr] & ~mask) | (data & mask)
        )

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, SwdWrite):
                if op.ap:
                    if op.addr == 0x00:  # CSW
                        self._csw = op.data
                    elif op.addr == 0x04:  # TAR
                        self._tar = op.data
                    elif op.addr == 0x0c:  # DRW write -> memory write
                        size_bits = self._csw & 0x7
                        size = {0: 1, 1: 2, 2: 4}[size_bits]
                        self._write_memory(self._tar, op.data, size)
                        self._tar += size
            elif isinstance(op, SwdRead):
                if not op.ap and op.addr == 0x0c:  # RDBUFF
                    op.data = self._pending_read
                elif op.ap and op.addr == 0x0c:  # DRW read (pipelined)
                    # Return previous read's data
                    op.data = self._pending_read
                    # Stage current read's data for next
                    self._pending_read = self._memory.get(self._tar & ~3, 0)
                    size_bits = self._csw & 0x7
                    size = {0: 1, 1: 2, 2: 4}[size_bits]
                    self._tar += size
                else:
                    op.data = 0
            future.set_result(op)


class TestMemRead:
    @pytest.mark.asyncio
    async def test_aligned_read(self):
        """Read 4 aligned bytes returns correct data."""
        iface = MemoryMockSwdInterface()
        iface.store_bytes(0x20000000, b"\x11\x22\x33\x44")
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        data = await memap.mem_read(0x20000000, 4)
        assert data == b"\x11\x22\x33\x44"

    @pytest.mark.asyncio
    async def test_multi_word_read(self):
        """Read spanning multiple words."""
        iface = MemoryMockSwdInterface()
        payload = bytes(range(16))
        iface.store_bytes(0x20000000, payload)
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        data = await memap.mem_read(0x20000000, 16)
        assert data == payload

    @pytest.mark.asyncio
    async def test_unaligned_read(self):
        """Read starting at non-word-aligned address."""
        iface = MemoryMockSwdInterface()
        iface.store_bytes(0x20000000, b"\xaa\xbb\xcc\xdd\xee\xff\x11\x22")
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        # Read 3 bytes starting at offset 1
        data = await memap.mem_read(0x20000001, 3)
        assert data == b"\xbb\xcc\xdd"

    @pytest.mark.asyncio
    async def test_single_byte_read(self):
        """Read a single byte."""
        iface = MemoryMockSwdInterface()
        iface.store_bytes(0x20000002, b"\x42")
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        data = await memap.mem_read(0x20000002, 1)
        assert data == b"\x42"

    @pytest.mark.asyncio
    async def test_empty_read(self):
        """Read 0 bytes."""
        iface = MemoryMockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        data = await memap.mem_read(0x20000000, 0)
        assert data == b""


class TestMemWrite:
    @pytest.mark.asyncio
    async def test_aligned_write(self):
        """Write 4 aligned bytes."""
        iface = MemoryMockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        await memap.mem_write(0x20000000, b"\x11\x22\x33\x44")

        # Check write log — should be a single u32 write
        assert len(iface.write_log) == 1
        addr, val, size = iface.write_log[0]
        assert addr == 0x20000000
        assert size == 4

    @pytest.mark.asyncio
    async def test_unaligned_write_odd_start(self):
        """Write starting at odd address peels u8 first."""
        iface = MemoryMockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        await memap.mem_write(0x20000001, b"\xaa\xbb\xcc")

        # First op should be a 1-byte write at 0x20000001
        addr0, val0, size0 = iface.write_log[0]
        assert addr0 == 0x20000001
        assert size0 == 1
        assert val0 == 0xaa

    @pytest.mark.asyncio
    async def test_unaligned_write_half_aligned(self):
        """Write at 2-byte aligned but not 4-byte aligned address."""
        iface = MemoryMockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        await memap.mem_write(0x20000002, b"\xaa\xbb\xcc\xdd\xee\xff")

        # Should peel u16 at start, then u32 middle
        addr0, val0, size0 = iface.write_log[0]
        assert addr0 == 0x20000002
        assert size0 == 2

    @pytest.mark.asyncio
    async def test_trailing_bytes(self):
        """Write with trailing u16 and u8."""
        iface = MemoryMockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        # 7 bytes at aligned addr: 4 + 2 + 1
        await memap.mem_write(0x20000000, b"\x01\x02\x03\x04\x05\x06\x07")

        sizes = [s for _, _, s in iface.write_log]
        assert sizes == [4, 2, 1]

    @pytest.mark.asyncio
    async def test_empty_write(self):
        """Write empty data is a no-op."""
        iface = MemoryMockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        await memap.mem_write(0x20000000, b"")
        assert len(iface.write_log) == 0

    @pytest.mark.asyncio
    async def test_roundtrip(self):
        """Write then read back matches."""
        iface = MemoryMockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        payload = b"\xde\xad\xbe\xef\xca\xfe\xba\xbe"
        await memap.mem_write(0x20000000, payload)

        # Reset TAR tracking for fresh reads
        memap._tar = None

        data = await memap.mem_read(0x20000000, 8)
        assert data == payload

    @pytest.mark.asyncio
    async def test_single_byte_write(self):
        """Write a single byte."""
        iface = MemoryMockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        await memap.mem_write(0x20000003, b"\xff")

        assert len(iface.write_log) == 1
        addr, val, size = iface.write_log[0]
        assert addr == 0x20000003
        assert size == 1
        assert val == 0xff
