"""ARM Memory Access Port (MEM-AP).

A MEM-AP bridges the DP into a connected memory system (AHB / APB /
AXI / AHB5 / AXI5). It implements the whole
:mod:`acrobe.protocol.memory` surface: the register family lowers to
AP register accesses (CSW / TAR / DRW) with intra-batch state caching
so redundant CSW / TAR writes are skipped and sequential same-size
streams ride the hardware's TAR auto-increment; the bulk family is
served on top of it by :class:`~acrobe.protocol.memory.BulkFromRegister`.

Blob reads use the overwide decomposition: the ARM memory model has no
read side effects worth protecting in an aperture the range already
touches, so covering the range with aligned 32-bit reads is both
cheaper (one access size, no CSW churn) and friendlier to TAR
auto-increment than peeling exact edges.

Layout-agnostic lowering lives in :class:`MemApLowering`. Two
flavours of register layout subclass it:

* :class:`MemAp` — APv1 / ADIv5: CSW=0x00, TAR=0x04, DRW=0x0c, BASE=0xF8,
  IDR=0xFC. AP-register access via the inherited ``Ap.reg_read`` /
  ``Ap.reg_write`` (one ApRead/ApWrite to the parent DP per call).
  Discovered via APSEL walk, registered against ``Ap.db``.
* :class:`MemApV2` — APv2 / ADIv6: same registers shifted into the
  0xD00..0xDFC management area. AP-register access via
  ``self._bus.read32`` / ``self._bus.write32`` (typically a
  :class:`DpSystemBus`). Discovered through CoreSight ROM Table walks
  (registered against ``MemoryMappedComponent.devarch_db`` for
  ARCHID 0x0A17).
"""

from __future__ import annotations

import asyncio

from ...engine import Batcher
from ...protocol.memory import (
    BulkFromRegister, Interface, OverwideDecomposition,
    Read8, Read16, Read32, Write8, Write16, Write32,
)
from . import dp as dpmod
from .ap import Ap, ApIdr
from .coresight.model import (
    DevArch, MemoryMappedComponent,
)


class MemApLowering(BulkFromRegister):
    """Mixin: register-layout-agnostic MEM-AP lowering, shared by
    APv1 (:class:`MemAp`) and APv2 (:class:`MemApV2`).

    Owns CSW/CFG bit constants, size lookup tables, the register-op
    wire step, and the start-time CFG/BASE parsing + ROM Table
    discovery.

    Concrete subclasses provide:

    * Register-offset class attributes: ``CSW``, ``TAR_LO``,
      ``TAR_HI``, ``DRW``, ``BASE_HI``, ``CFG``, ``BASE_LO``,
      ``IDR``.
    * ``reg_read(reg) -> Future[int]`` and
      ``reg_write(reg, data) -> Future[None]`` for one-shot 32-bit
      AP-register access at offset ``self.base + reg``. APv1 inherits
      these from :class:`Ap`; APv2 routes them through
      :class:`DpSystemBus`."""

    ops = Interface.REGISTER_OPS | Interface.BULK_OPS
    decomposition = OverwideDecomposition

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

    # Address-space size of this MEM-AP's connected bus, in bits.
    # Upgraded to 64 in start() if CFG.LA is set (LPAE). Used by the
    # CoreSight ROM Table walker to mask computed child addresses.
    addr_size_bits: int = 32

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

    @staticmethod
    def __size_for(op) -> int:
        if isinstance(op, (Read8, Write8)):
            return MemApLowering.CSW_SIZE_BYTE
        if isinstance(op, (Read16, Write16)):
            return MemApLowering.CSW_SIZE_HALF
        if isinstance(op, (Read32, Write32)):
            return MemApLowering.CSW_SIZE_WORD
        raise TypeError(f"Unhandled MEM-AP op: {type(op).__name__}")

    def __csw_with(self, size: int, addrinc: int) -> int:
        """Build a CSW value: requested size + auto-increment, plus
        the master-mode bits the AP needs for normal memory access.

        AHB-AP / AHB5-AP need MasterType=Debug + HPROT[0..1] for the
        Cortex-M PPB region; APB-AP and other types ignore those
        upper bits."""
        return (self.CSW_DBGSWENABLE
                | self.CSW_MASTER_DEBUG
                | self.CSW_HPROT1_BUFF
                | self.CSW_HPROT0_PRIV
                | self.CSW_DEVICE_EN
                | (addrinc & 0b11_0000)
                | (size & 0b111))

    # -- Register-op wire step ------------------------------------

    @staticmethod
    def __consume_exception(fut: asyncio.Future) -> None:
        """No-op done callback that just retrieves any exception so
        asyncio doesn't log a 'Future exception was never retrieved'
        warning. Used on CSW/TAR write futures whose result we don't
        surface — wire faults still surface via the trailing DRW
        future, which IS chained to a user future."""
        fut.exception()

    def __chain_read_lane(self, op, user_future: asyncio.Future,
                         drw_future: asyncio.Future) -> None:
        """When the DRW read resolves, extract the byte/halfword lane
        and resolve ``user_future`` with the masked value (or
        propagate the exception)."""
        size_bytes = self._SIZE_BYTES[self.__size_for(op)]
        shift = (op.addr & 3) * 8
        mask = (1 << (size_bytes * 8)) - 1

        def cb(fut: asyncio.Future) -> None:
            if user_future.done():
                return
            try:
                raw = fut.result()
            except Exception as exc:
                user_future.set_exception(exc)
                return
            user_future.set_result((raw >> shift) & mask)
        drw_future.add_done_callback(cb)

    @staticmethod
    def __chain_write_completion(user_future: asyncio.Future,
                                drw_future: asyncio.Future) -> None:
        """When the DRW write resolves, resolve ``user_future`` with
        ``None`` (or propagate the exception). Writes have no payload
        — the future just signals committal on the wire."""
        def cb(fut: asyncio.Future) -> None:
            if user_future.done():
                return
            try:
                fut.result()
            except Exception as exc:
                user_future.set_exception(exc)
                return
            user_future.set_result(None)
        drw_future.add_done_callback(cb)

    def lower_register_ops(self, batch):
        """Emit one CSW/TAR/DRW set per op, skipping CSW and TAR
        writes the previous op already established.

        The cache starts empty on every batch: an interleaved batch
        from another Mem-AP user — or any other DP/AP consumer that
        touches AP state — may have moved CSW and TAR since we last
        wrote them."""
        csw = None
        tar = None
        for op, user_future in batch:
            csw, tar = self.__emit(op, csw, tar, user_future)

    def __csw_tar_setup(self, addr: int, size_field: int,
                       csw, tar):
        """Issue CSW and TAR writes if their cached values don't match
        the requested ones. Returns the updated cache values."""
        new_csw = self.__csw_with(size=size_field,
                                 addrinc=self.CSW_ADDRINC_SINGLE)
        if csw != new_csw:
            f = self.reg_write(self.CSW, new_csw)
            f.add_done_callback(self.__consume_exception)
            csw = new_csw
        if tar != addr:
            f = self.reg_write(self.TAR_LO, addr & 0xffffffff)
            f.add_done_callback(self.__consume_exception)
            tar = addr
        return csw, tar

    @staticmethod
    def __tar_advance(addr: int, size_bytes: int, tar):
        """Auto-increment cached TAR after a write/read. If the next
        address crosses the 1 KiB segment boundary auto-inc no longer
        guarantees correctness, so invalidate the cache."""
        next_tar = addr + size_bytes
        if (next_tar & 0xFFFFFC00) != (addr & 0xFFFFFC00):
            return None
        return next_tar

    def __emit(self, op, csw, tar, user_future):
        """Emit one CSW/TAR/DRW set for a single Read*/Write* op and
        chain the user future to its DRW future."""
        try:
            size_field = self.__size_for(op)
        except TypeError as exc:
            if user_future is not None:
                user_future.set_exception(exc)
            return csw, tar
        csw, tar = self.__csw_tar_setup(op.addr, size_field, csw, tar)
        if isinstance(op, (Read8, Read16, Read32)):
            drw_future = self.reg_read(self.DRW)
            if user_future is None:
                drw_future.add_done_callback(self.__consume_exception)
            else:
                self.__chain_read_lane(op, user_future, drw_future)
        else:
            shift = (op.addr & 3) * 8
            lane_data = (op.data << shift) & 0xffffffff
            drw_future = self.reg_write(self.DRW, lane_data)
            if user_future is None:
                drw_future.add_done_callback(self.__consume_exception)
            else:
                self.__chain_write_completion(user_future, drw_future)
        tar = self.__tar_advance(op.addr, self._SIZE_BYTES[size_field], tar)
        return csw, tar

    # -- start() helpers ------------------------------------------

    async def _read_cfg_and_base(self) -> int | None:
        """Read CFG, then BASE_LO (and BASE_HI if CFG.LA). Sets
        ``self.cfg``, ``self.addr_size_bits`` (64 if LPAE), and
        ``self.base_addr`` (None if no debug components / sentinel).
        Returns ``self.base_addr``.

        BASE has two formats; FORMAT bit (bit[1]) selects:

          * FORMAT=1 (ADIv5.2 / ADIv6): bit[0]=P,
            bits[31:12]=base address.
          * FORMAT=0 (legacy ADIv5.0): bits[31:12]=base address
            unconditionally; whole register == 0xFFFFFFFF is the
            "no debug entry" sentinel.

        Both must be accepted — Zynq-7's APB-AP uses the legacy
        format and reports BASE = 0x80000000, which the new-format
        check (bit 0) would mis-read as P=0."""
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

        if base_lo == 0xFFFFFFFF:
            self.base_addr = None
            self.logger.info(
                "BASE 0xFFFFFFFF: no debug components (sentinel)")
            return None

        new_format = bool(base_lo & 0x2)
        present = bool(base_lo & 0x1) if new_format else True
        if not present:
            self.base_addr = None
            self.logger.info(
                "BASE 0x%08x: no debug components (P=0)", base_lo)
            return None

        self.base_addr = (
            ((base_hi & 0xffffffff) << 32) | (base_lo & 0xfffff000))
        self.logger.info(
            "BASE 0x%016x (%s format)", self.base_addr,
            "new" if new_format else "legacy")
        return self.base_addr

    async def _discover_base_component(self) -> None:
        """Discover the CoreSight component at ``self.base_addr``
        (typically a ROM Table) and attach as a child. Best-effort:
        a faulty BASE installs a :class:`PowerGate` placeholder. No-op
        if ``self.base_addr is None``."""
        if self.base_addr is None:
            return
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

        # The PIDR of the AP's root component — typically the top
        # ROM Table — also serves as the chip identifier on older
        # ADIs that don't implement TARGETID (DPv0 / DPv1). Log it
        # at info level so chip identity is always visible.
        if (isinstance(child, MemoryMappedComponent)
                and child.cidr_class is not None):
            self.logger.info(
                "BASE component: %s — %s",
                type(child).__name__, child.partid.pretty())

        self.child_add(child)


# --- APv1 / ADIv5 MEM-AP ------------------------------------------

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

@Ap.db.register(*[ApIdr.from_idr(_idr)
                  for _idr in (0x04770001, 0x04770002,
                               0x04770004, 0x04770005,
                               0x04770006, 0x04770007,
                               0x04770008)])
class MemAp(MemApLowering, Ap, Batcher):
    """APv1 / ADIv5 MEM-AP. AP-register access uses
    :meth:`Ap.reg_read` / :meth:`Ap.reg_write`, which post one
    ApRead/ApWrite to the parent DP per call."""

    # APv1 (ADIv5) register offsets.
    CSW       = 0x00
    TAR_LO    = 0x04
    TAR_HI    = 0x08  # only when CFG.LA == 1 (LPAE)
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

    def __init__(self, dp: dpmod.Dp, base: int, idr: int = 0,
                 name: str | None = None):
        # Friendlier default name based on IDR.TYPE.
        if name is None:
            type_name = self._type_name_for_idr(idr)
            if base & 0x00FFFFFF == 0:
                apsel = base >> 24
                name = (f"{type_name}@{apsel}" if type_name
                        else f"ap{apsel}")
            else:
                name = (f"{type_name}@{base:08x}" if type_name
                        else f"ap@{base:08x}")
        Ap.__init__(self, dp, base, idr=idr, name=name)
        Batcher.__init__(self)
        # BASE address (pointer into the AP's memory map at which the
        # ROM Table or single component lives). Populated in start().
        self.base_addr: int | None = None
        # CFG snapshot taken in start() — used by callers that need
        # to know about LPAE/LD support.
        self.cfg: int | None = None

    async def start(self):
        """Read CFG and BASE; if BASE is valid, kick off CoreSight
        discovery at that address by attaching the discovered
        component (typically a ROM Table) as our child."""
        await self._read_cfg_and_base()
        await self._discover_base_component()

    async def start_tree(self):
        """Best-effort tree start: a single child's failed start()
        is logged but doesn't drop siblings or block a power-gated
        sibling from later retry."""
        await self.ensure_started()
        for child in self.children:
            try:
                await child.start_tree()
            except Exception as exc:
                self.logger.warning(
                    "Child %r start failed: %s. Subtree incomplete.",
                    child.name, exc, exc_info=True)


# --- APv2 / ADIv6 MEM-AP ------------------------------------------

# DEVARCH for APv2 MEM-AP per IHI0074F: ARCHITECT=0x23B (ARM),
# ARCHID=0x0A17. devarch_db's eq func masks REVISION, so one
# registration covers all silicon revisions.
@MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x0A17, revision=0, present=True)
)
class MemApV2(MemApLowering, MemoryMappedComponent, Batcher):
    """APv2 / ADIv6 MEM-AP. Registered against
    ``MemoryMappedComponent.devarch_db`` keyed on ARCHID 0x0A17 so
    the CoreSight ROM Table walker instantiates one whenever it
    encounters a class-0x9 component with that DEVARCH.

    Differs from APv1 :class:`MemAp` only in:

    * Register offsets — CSW/TAR/DRW/CFG/BASE/IDR live in the
      management area (0xD00..0xDFC).
    * AP-register access — routed through ``self._bus`` (a
      :class:`DpSystemBus` for an AP attached directly to the DP's
      top-level ROM Table). One bus.read32/write32 = one APACC.

    Inheritance order: :class:`MemApLowering` first so its lowering
    machinery (flush_ops, op posters, blob expansion) wins over
    anything inherited; :class:`MemoryMappedComponent` for the
    Node-tree contract; :class:`Batcher` for ``post`` storage."""

    FRIENDLY_NAME = "MEM-AP"

    # APv2 register offsets — CSW/TAR/DRW shifted into the management
    # area.
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

    # ``reg_read`` / ``reg_write`` inherited from
    # :class:`MemoryMappedComponent`.

    async def start(self):
        """Read IDR/CFG/BASE; if BASE is valid, discover the component
        sitting in this AP's memory aperture (typically a ROM Table)
        and attach it as a child."""
        self.idr = await self.reg_read(self.IDR)
        type_name = self._type_name_for_idr(self.idr)
        self.logger.info(
            "IDR 0x%08x (%s)", self.idr, type_name or "unknown")
        # Now that we know the AP type, refine the Node name.
        if type_name and self.name.startswith("MEM-AP@"):
            self.name = f"{type_name}@{self.base:08x}"
        await self._read_cfg_and_base()
        await self._discover_base_component()

    # MemoryMappedComponent.start_tree provides the best-effort
    # behaviour we need; no override required.
