"""ARM Access Port (AP) and MEM-AP.

AP is a Component that reads/writes AP registers through the DP.
MemAp is a Batcher that translates memory read/write operations
into AP register operations with CSW/TAR/DRW state tracking.
"""

from __future__ import annotations

import asyncio
import struct

from ...engine import Batcher
from ...component import Component
from ...db import Db, NoMatch
from .dp import ApRead, ApWrite


# --- AP base ---

class Ap(Component):
    """Access Port. Routes register reads/writes through the parent DP."""

    IDR = 0xfc
    db = Db("AP type", eq_func=lambda a, b: not ((a ^ b) & 0x0fffe00f))

    def __init__(self, dp, index: int, name: str = None):
        if name is None:
            name = f"ap{index}"
        super().__init__(name)
        self._dp = dp
        self.index = index
        self._idr = None

    def reg_read(self, addr: int):
        """Post AP register read. Returns Future -> int."""
        return self._dp.post(ApRead(self.index, addr))

    def reg_write(self, addr: int, data: int):
        """Post AP register write. Returns Future -> None."""
        return self._dp.post(ApWrite(self.index, addr, data))

    async def read_idr(self) -> int:
        """Read the AP Identification Register."""
        self._idr = await self.reg_read(self.IDR)
        return self._idr

    async def cast(self):
        """Read IDR and return a type-specific AP if registered."""
        idr = await self.read_idr()
        try:
            handlers = self.db.get(idr)
            return handlers[0](self._dp, self.index, idr)
        except NoMatch:
            return self

    def __repr__(self):
        return f"<Ap {self._name} index={self.index}>"


# --- MEM-AP ---

class MemRead:
    """Memory read operation for MemAp."""

    def __init__(self, addr: int, size: int = 4):
        self.addr = addr
        self.size = size  # 1, 2, or 4 bytes
        self.data = None

    def __repr__(self):
        return f"<MemRead {self.addr:#010x} {self.size}B>"


class MemWrite:
    """Memory write operation for MemAp."""

    def __init__(self, addr: int, data: int, size: int = 4):
        self.addr = addr
        self.data = data
        self.size = size  # 1, 2, or 4 bytes

    def __repr__(self):
        return f"<MemWrite {self.addr:#010x} = {self.data:#x} {self.size}B>"


class MemAp(Ap, Batcher):
    """MEM-AP (AHB/APB/AXI). Translates memory accesses into AP register ops.

    Tracks CSW and TAR state to minimize redundant writes.
    Supports auto-increment for sequential word reads/writes.
    """

    # Register offsets
    CSW = 0x00
    TAR = 0x04
    DRW = 0x0c
    BASE = 0xf8
    CFG = 0xf4

    # CSW fields
    CSW_SIZE_BYTE = 0
    CSW_SIZE_HALF = 1
    CSW_SIZE_WORD = 2
    CSW_ADDRINC_OFF = 0 << 4
    CSW_ADDRINC_SINGLE = 1 << 4
    CSW_ADDRINC_PACKED = 2 << 4
    CSW_DBGSWENABLE = 1 << 31

    SIZE_MAP = {1: CSW_SIZE_BYTE, 2: CSW_SIZE_HALF, 4: CSW_SIZE_WORD}

    def __init__(self, dp, index: int, idr: int = 0, name: str = None):
        if name is None:
            name = f"memap{index}"
        Ap.__init__(self, dp, index, name)
        Batcher.__init__(self)
        self._idr = idr
        self._csw = None  # tracked CSW value
        self._tar = None  # tracked TAR value
        self.base_addr = 0

    async def start(self):
        """Read BASE register and enable debug access."""
        self._csw = self.CSW_DBGSWENABLE | self.CSW_SIZE_WORD | self.CSW_ADDRINC_SINGLE
        await self.reg_write(self.CSW, self._csw)
        base = await self.reg_read(self.BASE)
        self.base_addr = base & 0xfffff000
        self.logger.info("MEM-AP base: 0x%08x", self.base_addr)

    def read32(self, addr: int):
        """Post a 32-bit memory read. Returns Future -> int."""
        return self.post(MemRead(addr, 4))

    def write32(self, addr: int, data: int):
        """Post a 32-bit memory write. Returns Future -> None."""
        return self.post(MemWrite(addr, data, 4))

    def read_block(self, addr: int, word_count: int):
        """Post multiple 32-bit reads. Returns Future -> list[int]."""
        return self.post(("read_block", addr, word_count))

    async def mem_read(self, addr: int, size: int) -> bytes:
        """Read arbitrary bytes from memory.

        Rounds to 4-byte aligned word reads and slices out the
        requested range.
        """
        before = addr & 3
        end = addr + size
        after = (-end) & 3  # padding to next word boundary

        word_count = (before + size + after) // 4
        words = await self.read_block(addr - before, word_count)

        blob = b"".join(struct.pack("<I", w) for w in words)
        return blob[before:before + size]

    async def mem_write(self, addr: int, data: bytes):
        """Write arbitrary bytes to memory.

        Peels off unaligned head/tail as u8/u16 writes, bulk-writes
        aligned words in the middle.
        """
        if not data:
            return

        futures = []
        offset = 0

        # Peel odd byte at start
        if addr & 1:
            futures.append(self.post(MemWrite(addr, data[offset], size=1)))
            addr += 1
            offset += 1

        # Peel u16 for 2-byte alignment
        remaining = len(data) - offset
        if remaining >= 2 and (addr & 2):
            val = struct.unpack_from("<H", data, offset)[0]
            futures.append(self.post(MemWrite(addr, val, size=2)))
            addr += 2
            offset += 2

        # Bulk u32 writes
        remaining = len(data) - offset
        bulk = remaining & ~3
        for i in range(0, bulk, 4):
            val = struct.unpack_from("<I", data, offset + i)[0]
            futures.append(self.post(MemWrite(addr + i, val, size=4)))
        addr += bulk
        offset += bulk

        # Trailing u16
        remaining = len(data) - offset
        if remaining >= 2:
            val = struct.unpack_from("<H", data, offset)[0]
            futures.append(self.post(MemWrite(addr, val, size=2)))
            addr += 2
            offset += 2

        # Trailing u8
        if offset < len(data):
            futures.append(self.post(MemWrite(addr, data[offset], size=1)))

        await asyncio.gather(*futures)

    async def flush_ops(self, batch):
        """Translate memory ops into AP register operations."""
        dp_futures = []
        result_map = []  # (dp_future, dp_batch_future, size, addr)

        for op, future in batch:
            if isinstance(op, tuple) and op[0] == "read_block":
                _, addr, word_count = op
                reads = []
                for i in range(word_count):
                    r = MemRead(addr + i * 4, 4)
                    reads.append(r)
                # Process each read
                read_futures = []
                for r in reads:
                    rf = self._emit_read(r, dp_futures, result_map)
                    read_futures.append((rf, r))
                # Resolve with list of values after gather
                future._block_reads = read_futures  # tag for later
                result_map.append(("block", future, read_futures))

            elif isinstance(op, MemRead):
                self._emit_read(op, dp_futures, result_map)
                result_map.append(("read", future, op))

            elif isinstance(op, MemWrite):
                self._emit_write(op, dp_futures)
                result_map.append(("write", future))

        # Await all DP operations
        if dp_futures:
            await asyncio.gather(*dp_futures)

        # Resolve results
        for entry in result_map:
            if entry[0] == "read":
                _, dp_future, mem_op = entry
                if not dp_future.done():
                    dp_future.set_result(mem_op.data)
            elif entry[0] == "block":
                _, dp_future, read_futures = entry
                values = []
                for rf, r in read_futures:
                    values.append(r.data)
                dp_future.set_result(values)
            elif entry[0] == "write":
                _, dp_future = entry
                dp_future.set_result(None)

    def _emit_read(self, op, dp_futures, result_map):
        """Emit AP operations for a memory read."""
        size = op.size
        csw_size = self.SIZE_MAP[size]
        needed_csw = (self.CSW_DBGSWENABLE | csw_size |
                      self.CSW_ADDRINC_SINGLE)

        if self._csw != needed_csw:
            f = self.reg_write(self.CSW, needed_csw)
            dp_futures.append(f)
            self._csw = needed_csw

        if self._tar != op.addr:
            f = self.reg_write(self.TAR, op.addr)
            dp_futures.append(f)
            self._tar = op.addr

        f = self.reg_read(self.DRW)
        dp_futures.append(f)

        # Auto-increment TAR
        self._tar = (self._tar + size) if self._tar is not None else None

        # Extract read data when future resolves
        async def extract_data(read_future, mem_op, addr, sz):
            raw = await read_future
            shift = (addr & 3) * 8
            mask = (1 << (sz * 8)) - 1
            mem_op.data = (raw >> shift) & mask

        dp_futures.append(asyncio.ensure_future(extract_data(f, op, op.addr, size)))
        return f

    def _emit_write(self, op, dp_futures):
        """Emit AP operations for a memory write."""
        size = op.size
        csw_size = self.SIZE_MAP[size]
        needed_csw = (self.CSW_DBGSWENABLE | csw_size |
                      self.CSW_ADDRINC_SINGLE)

        if self._csw != needed_csw:
            f = self.reg_write(self.CSW, needed_csw)
            dp_futures.append(f)
            self._csw = needed_csw

        if self._tar != op.addr:
            f = self.reg_write(self.TAR, op.addr)
            dp_futures.append(f)
            self._tar = op.addr

        # Shift data into correct lane
        shift = (op.addr & 3) * 8
        data = (op.data << shift) & 0xffffffff

        f = self.reg_write(self.DRW, data)
        dp_futures.append(f)

        # Auto-increment TAR
        self._tar = (self._tar + size) if self._tar is not None else None

    def __repr__(self):
        return f"<MemAp {self._name} index={self.index} base={self.base_addr:#010x}>"
