"""ARM Memory Access Port (MEM-AP).

A MEM-AP bridges the DP into a connected memory system (AHB / APB /
AXI / AHB5 / AXI5). MemAp is a Batcher whose operations are
``Read{8,16,32}`` / ``Write{8,16,32}``; flush_ops translates these
into AP register accesses (CSW / TAR / DRW) on the parent DP, with
state caching to skip redundant CSW / TAR writes and TAR
auto-incrementing for sequential same-size streams.

Also exposes ``mem_read(addr, size) -> bytes`` and
``mem_write(addr, data)`` convenience coroutines for byte-granular
access; these decompose unaligned head/tail into byte/halfword
accesses and bulk-handle aligned middles as word streams.

This implementation targets APv1 (ADIv5) register layout (CSW=0x00,
TAR=0x04, DRW=0x0c, BASE=0xF8, IDR=0xFC). The APv2 (ADIv6) layout
puts these registers at 0xD00..0xD0C / 0xDF8 / 0xDFC; a separate
``MemApV2`` will land alongside DPv3 wire support.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from ...engine import Batcher
from . import dp as dpmod
from .ap import Ap


# --- Op dataclasses (frozen, inputs only) --------------------------

@dataclass(frozen=True)
class Read8:
    addr: int


@dataclass(frozen=True)
class Read16:
    addr: int


@dataclass(frozen=True)
class Read32:
    addr: int


@dataclass(frozen=True)
class Write8:
    addr: int
    data: int


@dataclass(frozen=True)
class Write16:
    addr: int
    data: int


@dataclass(frozen=True)
class Write32:
    addr: int
    data: int


# --- MEM-AP --------------------------------------------------------

class MemAp(Ap, Batcher):
    """MEM-AP base class. Concrete bus types (AHB / APB / AXI) all
    use the same register layout and operation flow; they're
    distinguished only by IDR.TYPE.

    Pure word-mode chips can be modelled by subclassing and pinning
    the supported sizes in ``_csw_for_size``."""

    # APv1 (ADIv5) register offsets.
    CSW       = 0x00
    TAR_LO    = 0x04
    TAR_HI    = 0x08  # only when CFG.LA == 1 (Large Physical Address Extension)
    DRW       = 0x0c
    BD0       = 0x10
    BD1       = 0x14
    BD2       = 0x18
    BD3       = 0x1c
    MBT       = 0x20
    BASE_HI   = 0xF0  # only when LPAE
    CFG       = 0xF4
    BASE_LO   = 0xF8
    # IDR is at 0xFC, defined on Ap base.

    # CSW field bits.
    CSW_SIZE_BYTE   = 0b000
    CSW_SIZE_HALF   = 0b001
    CSW_SIZE_WORD   = 0b010
    CSW_ADDRINC_OFF    = 0b00 << 4
    CSW_ADDRINC_SINGLE = 0b01 << 4
    CSW_ADDRINC_PACKED = 0b10 << 4
    CSW_DEVICE_EN   = 1 << 6
    CSW_TR_IN_PROG  = 1 << 7
    CSW_DBGSWENABLE = 1 << 31

    # CFG bit fields (slim — only the ones we read at start).
    CFG_BIG_ENDIAN = 1 << 0  # Pre-ADIv5.2 (RAZ on modern chips)
    CFG_LA         = 1 << 1  # Large Physical Address Extension
    CFG_LD         = 1 << 2  # Large Data Extension

    # Bytes per access size.
    _SIZE_BYTES = {
        CSW_SIZE_BYTE: 1,
        CSW_SIZE_HALF: 2,
        CSW_SIZE_WORD: 4,
    }

    def __init__(self, dp: dpmod.Dp, base: int, idr: int = 0,
                 name: str | None = None):
        # Friendlier default name based on IDR.TYPE.
        if name is None:
            type_name = self._type_name_for_idr(idr)
            if base & 0x00FFFFFF == 0:
                apsel = base >> 24
                name = f"{type_name}@{apsel}" if type_name else f"ap{apsel}"
            else:
                name = f"{type_name}@{base:08x}" if type_name else f"ap@{base:08x}"
        Ap.__init__(self, dp, base, idr=idr, name=name)
        Batcher.__init__(self)
        self._csw_cache: int | None = None
        self._tar_cache: int | None = None
        # BASE address (pointer into the AP's memory map at which the
        # ROM Table or single component lives). Populated in start().
        self.base_addr: int | None = None
        # CFG snapshot taken in start() — used by callers that need
        # to know about LPAE/LD support.
        self.cfg: int | None = None

    @staticmethod
    def _type_name_for_idr(idr: int) -> str:
        type_field = idr & 0xf
        return {
            0x1: "AHB-AP",
            0x2: "APB-AP",
            0x4: "AXI-AP",
            0x5: "AHB5-AP",
            0x6: "APB4-AP",
            0x7: "AXI5-AP",
            0x8: "AHB5-AP-EnH",
        }.get(type_field, "")

    async def start(self):
        """Read CFG and BASE; if BASE is valid, kick off CoreSight
        discovery at that address by attaching the discovered
        component (typically a ROM Table) as our child."""
        self.cfg = await self.reg_read(self.CFG)
        self.logger.info("CFG 0x%08x (LA=%d, LD=%d)",
                         self.cfg,
                         (self.cfg >> 1) & 1,
                         (self.cfg >> 2) & 1)

        base_lo = await self.reg_read(self.BASE_LO)
        base_hi = 0
        if self.cfg & self.CFG_LA:
            base_hi = await self.reg_read(self.BASE_HI)

        # BASE encoding: bit[0] = P (present), bit[1] = format.
        # When P=0, no debug components are accessible via this AP.
        if not (base_lo & 0x1):
            self.base_addr = None
            self.logger.info("BASE: no debug components (P=0)")
            return

        self.base_addr = ((base_hi & 0xffffffff) << 32) | (base_lo & 0xfffff000)
        self.logger.info("BASE 0x%016x", self.base_addr)

        # Discover the component at BASE. Typically a ROM Table; can
        # also be a single CoreSight component (the spec permits
        # APs with one accessible component and no ROM Table).
        # Discovery is best-effort — a faulty BASE doesn't kill the
        # AP: we install a PowerGate placeholder.
        from .coresight.model import ComponentIds, MemoryMappedComponent
        from .coresight.power_gate import FailureKind, PowerGate

        try:
            child = await MemoryMappedComponent.discover(self, self.base_addr)
        except Exception as exc:
            self.logger.warning(
                "BASE component at 0x%x: discover failed: %s",
                self.base_addr, exc, exc_info=True)
            child = PowerGate(self, self.base_addr, FailureKind.FAULT)

        if (isinstance(child, MemoryMappedComponent)
                and child.cidr_class is None):
            self.logger.info(
                "BASE component at 0x%x: no CIDR preamble — installing PowerGate",
                self.base_addr)
            child = PowerGate(self, self.base_addr, FailureKind.EMPTY)

        self.child_add(child)

    async def start_tree(self):
        """Best-effort tree start: a single child's failed start()
        is logged but doesn't drop siblings or block a power-gated
        sibling from later retry."""
        if not self._started:
            await self.start()
            self._started = True
        for child in self._children:
            try:
                await child.start_tree()
            except Exception as exc:
                self.logger.warning(
                    "Child %r start failed: %s. Subtree incomplete.",
                    child.name, exc, exc_info=True)

    # -- Op posting (Batcher API) -----------------------------------

    def read8(self, addr: int):
        return self.post(Read8(addr=addr))

    def read16(self, addr: int):
        return self.post(Read16(addr=addr))

    def read32(self, addr: int):
        return self.post(Read32(addr=addr))

    def write8(self, addr: int, data: int):
        return self.post(Write8(addr=addr, data=data))

    def write16(self, addr: int, data: int):
        return self.post(Write16(addr=addr, data=data))

    def write32(self, addr: int, data: int):
        return self.post(Write32(addr=addr, data=data))

    # -- Convenience: byte-granular memory access ------------------

    async def mem_read(self, addr: int, size: int) -> bytes:
        """Read ``size`` bytes from ``addr``. Decomposes unaligned
        head/tail into byte/halfword accesses; bulk-reads aligned
        middle as 32-bit words."""
        if size == 0:
            return b""
        return await self._byte_io(addr, size, data=None)

    async def mem_write(self, addr: int, data: bytes) -> None:
        if not data:
            return
        await self._byte_io(addr, len(data), data=data)

    async def _byte_io(self, addr: int, size: int,
                       data: bytes | None) -> bytes:
        """Unified head/middle/tail decomposition for read and write.

        Strategy:
          * Peel an unaligned leading byte (if addr % 2 == 1).
          * Peel a leading halfword (if addr % 4 == 2 and >= 2 bytes).
          * Stream the aligned middle as 32-bit words.
          * Peel a trailing halfword (if remaining >= 2).
          * Peel a trailing byte (if remaining == 1).

        Each peeled-off chunk is one MEM-AP op; the middle is one op
        per word. All ops are posted synchronously, then awaited
        together — the Batcher pipelines them through the DP."""
        is_write = data is not None
        offset = 0
        cursor = addr
        end = addr + size

        ops: list[tuple[str, int, int, asyncio.Future]] = []

        def peel(chunk_size: int):
            nonlocal cursor, offset
            if is_write:
                chunk = data[offset:offset + chunk_size]
                if chunk_size == 1:
                    f = self.write8(cursor, chunk[0])
                    ops.append(("w", cursor, 1, f))
                elif chunk_size == 2:
                    val = struct.unpack_from("<H", chunk)[0]
                    f = self.write16(cursor, val)
                    ops.append(("w", cursor, 2, f))
                else:  # 4
                    val = struct.unpack_from("<I", chunk)[0]
                    f = self.write32(cursor, val)
                    ops.append(("w", cursor, 4, f))
            else:
                if chunk_size == 1:
                    f = self.read8(cursor)
                elif chunk_size == 2:
                    f = self.read16(cursor)
                else:
                    f = self.read32(cursor)
                ops.append(("r", cursor, chunk_size, f))
            cursor += chunk_size
            offset += chunk_size

        # Leading byte to reach 2-aligned.
        if (cursor & 1) and end > cursor:
            peel(1)
        # Leading halfword to reach 4-aligned.
        if (cursor & 3) == 2 and end - cursor >= 2:
            peel(2)
        # Aligned middle: stream of 32-bit words.
        while end - cursor >= 4:
            peel(4)
        # Trailing halfword.
        if end - cursor >= 2:
            peel(2)
        # Trailing byte.
        if end - cursor >= 1:
            peel(1)

        # Await everything; gather is fine because all futures share
        # this MemAp's batcher (and the DP below).
        await asyncio.gather(*(o[3] for o in ops))

        if is_write:
            return None

        # Reconstruct output bytes from per-chunk reads.
        buf = bytearray(size)
        for kind, op_addr, sz, fut in ops:
            v = fut.result()
            chunk = v.to_bytes(sz, "little")
            buf_offset = op_addr - addr
            buf[buf_offset:buf_offset + sz] = chunk
        return bytes(buf)

    # -- Lowering --------------------------------------------------

    async def flush_ops(self, batch):
        # AP-level futures we need to await before resolving user
        # futures (CSW writes, TAR writes, DRW reads/writes).
        ap_futures: list[asyncio.Future] = []
        # (op, user_future, drw_future) for reads — we extract the
        # byte/halfword lane from drw_future.result() at the end.
        read_results: list[tuple[object, asyncio.Future, asyncio.Future]] = []

        csw = self._csw_cache
        tar = self._tar_cache

        for op, user_future in batch:
            try:
                size_field = self._size_for(op)
            except TypeError as exc:
                user_future.set_exception(exc)
                continue
            size_bytes = self._SIZE_BYTES[size_field]
            new_csw = self._csw_with(size=size_field,
                                     addrinc=self.CSW_ADDRINC_SINGLE)

            if csw != new_csw:
                ap_futures.append(self._dp.post(dpmod.ApWrite(
                    ap=self.base, addr=self.CSW, data=new_csw)))
                csw = new_csw
                # CSW change invalidates any auto-incremented TAR
                # assumption — the AP's TAR isn't actually re-issued,
                # but the size is changing, so be safe.
                # (In practice the AP's TAR retains its value across
                # CSW writes, but mixing sizes is rare and not worth
                # micro-optimising.)

            if tar != op.addr:
                ap_futures.append(self._dp.post(dpmod.ApWrite(
                    ap=self.base, addr=self.TAR_LO, data=op.addr & 0xffffffff)))
                tar = op.addr

            if isinstance(op, (Read8, Read16, Read32)):
                drw_future = self._dp.post(dpmod.ApRead(
                    ap=self.base, addr=self.DRW))
                ap_futures.append(drw_future)
                read_results.append((op, user_future, drw_future))
            else:
                # Write: shift data into the correct byte lane.
                shift = (op.addr & 3) * 8
                lane_data = (op.data << shift) & 0xffffffff
                ap_futures.append(self._dp.post(dpmod.ApWrite(
                    ap=self.base, addr=self.DRW, data=lane_data)))
                user_future.set_result(None)

            # Auto-increment cached TAR. If we'd cross a 1KB segment,
            # invalidate the cache — auto-inc only guarantees 10-bit
            # behavior (TARINC may say more, but we don't trust it
            # without explicit detection).
            next_tar = op.addr + size_bytes
            if (next_tar & 0xFFFFFC00) != (op.addr & 0xFFFFFC00):
                tar = None
            else:
                tar = next_tar

        self._csw_cache = csw
        self._tar_cache = tar

        if ap_futures:
            try:
                await asyncio.gather(*ap_futures)
            except Exception as exc:
                # Propagate to all unresolved user futures.
                for op, uf, _ in read_results:
                    if not uf.done():
                        uf.set_exception(exc)
                raise

        for op, user_future, drw_future in read_results:
            if user_future.done():
                continue
            raw = drw_future.result()
            shift = (op.addr & 3) * 8
            mask = (1 << (self._SIZE_BYTES[self._size_for(op)] * 8)) - 1
            user_future.set_result((raw >> shift) & mask)

    @staticmethod
    def _size_for(op) -> int:
        if isinstance(op, (Read8, Write8)):
            return MemAp.CSW_SIZE_BYTE
        if isinstance(op, (Read16, Write16)):
            return MemAp.CSW_SIZE_HALF
        if isinstance(op, (Read32, Write32)):
            return MemAp.CSW_SIZE_WORD
        raise TypeError(f"Unhandled MEM-AP op: {type(op).__name__}")

    def _csw_with(self, size: int, addrinc: int) -> int:
        """Build a CSW value with our standard PROT/Mode/etc.
        cleared, the requested size + auto-increment, and
        DbgSwEnable + DeviceEn set."""
        return (self.CSW_DBGSWENABLE
                | self.CSW_DEVICE_EN
                | (addrinc & 0b11_0000)
                | (size & 0b111))


# --- Registry against Ap.db ----------------------------------------
#
# IDR equality (Ap.db) masks REVISION (31:28) and VARIANT (7:4), so
# one registration per (DESIGNER, CLASS, TYPE) covers all silicon
# revisions and variants. Standard ARM-designer types:
#
#   0x04770001  AHB-AP   (TYPE=1)
#   0x04770002  APB-AP   (TYPE=2)
#   0x04770004  AXI-AP   (TYPE=4)
#   0x04770005  AHB5-AP  (TYPE=5)
#   0x04770006  APB4-AP  (TYPE=6)
#   0x04770007  AXI5-AP  (TYPE=7)
#   0x04770008  AHB5-AP with enhanced HPROT (TYPE=8)

for _idr in (0x04770001, 0x04770002, 0x04770004,
             0x04770005, 0x04770006, 0x04770007, 0x04770008):
    Ap.db.register(_idr)(MemAp)
