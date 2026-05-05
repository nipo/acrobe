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

Two flavours of MEM-AP register layout are supported:

* :class:`MemAp` — APv1 / ADIv5: CSW=0x00, TAR=0x04, DRW=0x0c, BASE=0xF8,
  IDR=0xFC. Discovered via APSEL walk, registered against ``Ap.db``.
* :class:`MemApV2` — APv2 / ADIv6: same registers shifted into the
  0xD00..0xDFC management area. Discovered through CoreSight ROM
  Table walks (registered against ``MemoryMappedComponent.devarch_db``
  for ARCHID 0x0A17).
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from ...engine import Batcher
from . import dp as dpmod
from .ap import Ap, ApIdr
from .coresight.model import (
    DevArch, MemoryMappedComponent,
)


# --- Op dataclasses (frozen, inputs only) --------------------------

@dataclass(frozen=True)
class Read8:
    addr: int

    def __repr__(self):
        return f"Read8({self.addr:#x})"

@dataclass(frozen=True)
class Read16:
    addr: int

    def __repr__(self):
        return f"Read16({self.addr:#x})"

@dataclass(frozen=True)
class Read32:
    addr: int

    def __repr__(self):
        return f"Read32({self.addr:#x})"

@dataclass(frozen=True)
class Write8:
    addr: int
    data: int

    def __repr__(self):
        return f"Write8({self.addr:#x}, {self.data:#04x})"

@dataclass(frozen=True)
class Write16:
    addr: int
    data: int

    def __repr__(self):
        return f"Write16({self.addr:#x}, {self.data:#06x})"

@dataclass(frozen=True)
class Write32:
    addr: int
    data: int

    def __repr__(self):
        return f"Write32({self.addr:#x}, {self.data:#010x})"

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
    CSW_DEVICE_EN     = 1 << 6
    CSW_TR_IN_PROG    = 1 << 7
    # AHB-AP CSW upper bits — needed for Cortex-M PPB access via the
    # system bus. OpenOCD writes these by default for AP type 1.
    CSW_HPROT0_PRIV   = 1 << 24  # HPROT[0] = Privileged
    CSW_HPROT1_BUFF   = 1 << 25  # HPROT[1] = Bufferable
    CSW_MASTER_DEBUG  = 1 << 29  # MasterType = Debug
    CSW_DBGSWENABLE   = 1 << 31

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

    # Address-space size of this MEM-AP's connected bus, in bits.
    # 32 by default; ``start()`` upgrades to 64 when CFG.LA is set
    # (Large Physical Address Extension). Used by the CoreSight ROM
    # Table walker to mask computed child addresses.
    addr_size_bits: int = 32

    async def start(self):
        """Read CFG and BASE; if BASE is valid, kick off CoreSight
        discovery at that address by attaching the discovered
        component (typically a ROM Table) as our child."""
        self.cfg = await self.reg_read(self.CFG)
        self.logger.info("CFG 0x%08x (LA=%d, LD=%d)",
                         self.cfg,
                         (self.cfg >> 1) & 1,
                         (self.cfg >> 2) & 1)
        if self.cfg & self.CFG_LA:
            self.addr_size_bits = 64

        base_lo = await self.reg_read(self.BASE_LO)
        base_hi = 0
        if self.cfg & self.CFG_LA:
            base_hi = await self.reg_read(self.BASE_HI)

        # BASE has two formats. The selector is bit[1] (FORMAT):
        #
        #   FORMAT = 1 (ADIv5.2 / ADIv6 format):
        #     bit[0] = P  (1 = debug entry present, 0 = none)
        #     bits[31:12] = ROM Table base address
        #
        #   FORMAT = 0 (legacy ADIv5.0 format):
        #     bits[31:12] = ROM Table base address (unconditional)
        #     bits[11:0] = RAZ
        #     The whole register == 0xFFFFFFFF is the "no debug
        #     entry" sentinel; any other value means present.
        #
        # We must accept both — Zynq-7's APB-AP uses the legacy
        # format and reports BASE = 0x80000000, which the new-format
        # check (bit 0) would mis-read as "P=0, no entry".
        #
        # 0xFFFFFFFF deserves a special case: it's the legacy
        # "no entries" sentinel, and as a new-format value it
        # would parse to P=1 / FORMAT=1 / address=0xFFFFF000 which
        # is meaningless. Treat it as "no entries" regardless.
        if base_lo == 0xFFFFFFFF:
            self.base_addr = None
            self.logger.info("BASE 0xFFFFFFFF: no debug components (sentinel)")
            return

        new_format = bool(base_lo & 0x2)
        present = bool(base_lo & 0x1) if new_format else True

        if not present:
            self.base_addr = None
            self.logger.info(
                "BASE 0x%08x: no debug components (new format, P=0)",
                base_lo)
            return

        self.base_addr = ((base_hi & 0xffffffff) << 32) | (base_lo & 0xfffff000)
        self.logger.info(
            "BASE 0x%016x (%s format)", self.base_addr,
            "new" if new_format else "legacy")

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

        # The PIDR of the AP's root component — typically the top
        # ROM Table — also serves as the chip identifier on older
        # ADIs that don't implement TARGETID (DPv0 / DPv1). Log it
        # here regardless of DP version so the chip identity is
        # always visible at info level.
        if (isinstance(child, MemoryMappedComponent)
                and child.cidr_class is not None):
            self.logger.info(
                "BASE component: %s — %s",
                type(child).__name__, child.partid.pretty())

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
                    addr=self.base + self.CSW, data=new_csw)))
                csw = new_csw
                # CSW change invalidates any auto-incremented TAR
                # assumption — the AP's TAR isn't actually re-issued,
                # but the size is changing, so be safe.
                # (In practice the AP's TAR retains its value across
                # CSW writes, but mixing sizes is rare and not worth
                # micro-optimising.)

            if tar != op.addr:
                ap_futures.append(self._dp.post(dpmod.ApWrite(
                    addr=self.base + self.TAR_LO,
                    data=op.addr & 0xffffffff)))
                tar = op.addr

            if isinstance(op, (Read8, Read16, Read32)):
                drw_future = self._dp.post(dpmod.ApRead(
                    addr=self.base + self.DRW))
                ap_futures.append(drw_future)
                read_results.append((op, user_future, drw_future))
            else:
                # Write: shift data into the correct byte lane.
                shift = (op.addr & 3) * 8
                lane_data = (op.data << shift) & 0xffffffff
                ap_futures.append(self._dp.post(dpmod.ApWrite(
                    addr=self.base + self.DRW, data=lane_data)))
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
        """Build a CSW value: requested size + auto-increment, plus
        the master-mode bits the AP needs for normal memory access.

        For AHB-AP / AHB5-AP we set MasterType=Debug and
        HPROT[0..1] (Privileged + Bufferable), matching OpenOCD's
        default for Cortex-M debug. Without MasterType=Debug, the
        Cortex-M's PPB region (0xE0000000-0xE00FFFFF) is unreachable
        via the AP — accesses get FAULTed with STICKYERR set. APB-AP
        and other types ignore those upper bits."""
        return (self.CSW_DBGSWENABLE
                | self.CSW_MASTER_DEBUG
                | self.CSW_HPROT1_BUFF
                | self.CSW_HPROT0_PRIV
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
    Ap.db.register(ApIdr.from_idr(_idr))(MemAp)


# --- ADIv6 / APv2 MEM-AP -------------------------------------------

class MemApV2(MemoryMappedComponent, Batcher):
    """ADIv6 / APv2 MEM-AP. Registered against
    ``MemoryMappedComponent.devarch_db`` keyed on ARCHID 0x0A17 so
    the CoreSight ROM Table walker instantiates one whenever it
    encounters a class-0x9 component with that DEVARCH.

    Same memory-access semantics as :class:`MemAp` (CSW/TAR/DRW dance,
    state caching, auto-increment, byte-granular ``mem_read`` /
    ``mem_write``); differs in:

    * **Register offsets**: CSW/TAR/DRW/CFG/BASE/IDR live in the
      management area (0xD00..0xDFC), versus APv1's 0x00..0xFC.
    * **Discovery**: instantiated from a ROM Table walk via
      ``devarch_db``, not from an APSEL walk against ``Ap.db``.
    * **Bus**: AP-register accesses go through ``self._bus`` (a
      :class:`DpSystemBus` for an AP attached directly to the DP's
      top-level ROM Table), instead of ``self._dp.post(ApRead/ApWrite)``.
      Same wire effect — the bus's ``read32``/``write32`` lower to one
      APACC each.

    Inheritance order: :class:`MemoryMappedComponent` first so its
    Node-tree contract (``__init__(bus, base, ids)``, child management,
    CIDR/PIDR accessors) governs; :class:`Batcher` second supplies
    ``post`` and ``flush_ops`` for the MEM-AP ops."""

    FRIENDLY_NAME = "MEM-AP"

    # APv2 register offsets — CSW/TAR/DRW shifted into the management area.
    CSW       = 0xD00
    TAR_LO    = 0xD04
    TAR_HI    = 0xD08
    DRW       = 0xD0C
    BD0       = 0xD10
    BD1       = 0xD14
    BD2       = 0xD18
    BD3       = 0xD1C
    MBT       = 0xD20
    BASE_HI   = 0xDF0
    CFG       = 0xDF4
    BASE_LO   = 0xDF8
    IDR       = 0xDFC

    # Reuse APv1's CSW bit definitions, CSW size table, and helpers.
    CSW_SIZE_BYTE      = MemAp.CSW_SIZE_BYTE
    CSW_SIZE_HALF      = MemAp.CSW_SIZE_HALF
    CSW_SIZE_WORD      = MemAp.CSW_SIZE_WORD
    CSW_ADDRINC_OFF    = MemAp.CSW_ADDRINC_OFF
    CSW_ADDRINC_SINGLE = MemAp.CSW_ADDRINC_SINGLE
    CSW_ADDRINC_PACKED = MemAp.CSW_ADDRINC_PACKED
    CSW_DEVICE_EN      = MemAp.CSW_DEVICE_EN
    CSW_TR_IN_PROG     = MemAp.CSW_TR_IN_PROG
    CSW_HPROT0_PRIV    = MemAp.CSW_HPROT0_PRIV
    CSW_HPROT1_BUFF    = MemAp.CSW_HPROT1_BUFF
    CSW_MASTER_DEBUG   = MemAp.CSW_MASTER_DEBUG
    CSW_DBGSWENABLE    = MemAp.CSW_DBGSWENABLE

    CFG_BIG_ENDIAN = MemAp.CFG_BIG_ENDIAN
    CFG_LA         = MemAp.CFG_LA
    CFG_LD         = MemAp.CFG_LD

    _SIZE_BYTES = MemAp._SIZE_BYTES

    # The lowering uses self._bus directly, but _size_for / _csw_with
    # are pure helpers reusable across MemAp variants.
    _size_for         = staticmethod(MemAp._size_for)
    _csw_with         = MemAp._csw_with
    _type_name_for_idr = staticmethod(MemAp._type_name_for_idr)

    def __init__(self, bus, base: int, ids,
                 name: str | None = None):
        # Defer IDR.TYPE-aware naming to start() once IDR is read;
        # __init__ runs synchronously from the ROM walker.
        if name is None:
            name = f"MEM-AP@{base:08x}"
        MemoryMappedComponent.__init__(self, bus, base, ids, name=name)
        Batcher.__init__(self)
        self.idr: int | None = None
        self.cfg: int | None = None
        self.base_addr: int | None = None
        self._csw_cache: int | None = None
        self._tar_cache: int | None = None

    # See :attr:`MemAp.addr_size_bits`.
    addr_size_bits: int = 32

    async def start(self):
        """Read IDR/CFG/BASE; if BASE is valid, discover the component
        sitting in this AP's memory aperture (typically a ROM Table)
        and attach it as a child."""
        self.idr = await self._bus.read32(self.base + self.IDR)
        self.cfg = await self._bus.read32(self.base + self.CFG)
        type_name = self._type_name_for_idr(self.idr)
        self.logger.info(
            "IDR 0x%08x (%s) CFG 0x%08x (LA=%d, LD=%d)",
            self.idr, type_name or "unknown",
            self.cfg, (self.cfg >> 1) & 1, (self.cfg >> 2) & 1)
        if self.cfg & self.CFG_LA:
            self.addr_size_bits = 64
        # Now that we know the AP type, refine the Node name.
        if type_name and self._name.startswith("MEM-AP@"):
            self._name = f"{type_name}@{self.base:08x}"

        base_lo = await self._bus.read32(self.base + self.BASE_LO)
        base_hi = 0
        if self.cfg & self.CFG_LA:
            base_hi = await self._bus.read32(self.base + self.BASE_HI)

        # BASE format handling — same logic as APv1: bit[0]=P,
        # bit[1]=FORMAT (0=legacy, 1=new), bits[31:12]=address.
        # 0xFFFFFFFF is the legacy "no debug entries" sentinel.
        if base_lo == 0xFFFFFFFF:
            self.base_addr = None
            self.logger.info("BASE 0xFFFFFFFF: no debug components (sentinel)")
            return

        new_format = bool(base_lo & 0x2)
        present = bool(base_lo & 0x1) if new_format else True
        if not present:
            self.base_addr = None
            self.logger.info(
                "BASE 0x%08x: no debug components (P=0)", base_lo)
            return

        self.base_addr = ((base_hi & 0xffffffff) << 32) | (base_lo & 0xfffff000)
        self.logger.info(
            "BASE 0x%016x (%s format)", self.base_addr,
            "new" if new_format else "legacy")

        # Walk the AP's memory aperture for a ROM Table / single
        # component. This is *memory* access — goes through this MEM-AP's
        # CSW/TAR/DRW dance via ``self`` as the bus.
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
                "BASE component at 0x%x: no CIDR preamble — "
                "installing PowerGate", self.base_addr)
            child = PowerGate(self, self.base_addr, FailureKind.EMPTY)

        if (isinstance(child, MemoryMappedComponent)
                and child.cidr_class is not None):
            self.logger.info(
                "BASE component: %s — %s",
                type(child).__name__, child.partid.pretty())

        self.child_add(child)

    # MemoryMappedComponent.start_tree provides the best-effort
    # behaviour we need; no override required.

    # -- User-facing memory access (mirrors MemAp) ------------------

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

    # Byte-granular convenience helpers — the implementation only
    # depends on read{8,16,32} / write{8,16,32}, so we delegate.
    # ``MemAp._byte_io`` is the byte-granular decomposer; it only
    # touches ``self.read{8,16,32}``/``self.write{8,16,32}`` so it
    # works with any object that exposes those — including a
    # ``MemApV2`` which doesn't inherit from ``MemAp``. Bind the
    # unbound method to keep one implementation.
    async def mem_read(self, addr: int, size: int) -> bytes:
        if size == 0:
            return b""
        return await MemAp._byte_io(self, addr, size, data=None)

    async def mem_write(self, addr: int, data: bytes) -> None:
        if not data:
            return
        await MemAp._byte_io(self, addr, len(data), data=data)

    # -- Lowering ---------------------------------------------------

    async def flush_ops(self, batch):
        """Translate batched MEM-AP ops to AP-register accesses on
        the bus. Mirrors :meth:`MemAp.flush_ops`; the only difference
        is the access path: ``self._bus.read32/write32`` here vs.
        ``self._dp.post(ApRead/ApWrite)`` there. Same wire effect."""
        ap_futures: list[asyncio.Future] = []
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
                ap_futures.append(self._bus.write32(
                    self.base + self.CSW, new_csw))
                csw = new_csw

            if tar != op.addr:
                ap_futures.append(self._bus.write32(
                    self.base + self.TAR_LO, op.addr & 0xffffffff))
                tar = op.addr

            if isinstance(op, (Read8, Read16, Read32)):
                drw_future = self._bus.read32(self.base + self.DRW)
                ap_futures.append(drw_future)
                read_results.append((op, user_future, drw_future))
            else:
                shift = (op.addr & 3) * 8
                lane_data = (op.data << shift) & 0xffffffff
                ap_futures.append(self._bus.write32(
                    self.base + self.DRW, lane_data))
                user_future.set_result(None)

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


# DEVARCH for APv2 MEM-AP per IHI0074F: ARCHITECT=0x23B (ARM),
# ARCHID=0x0A17. devarch_db's eq func masks REVISION, so one
# registration covers all silicon revisions.
MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x0A17, revision=0, present=True)
)(MemApV2)
