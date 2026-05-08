"""ARM Debug Port (DP).

Abstract DP layer shared by JTAG-DP and SW-DP. Operations are frozen
dataclasses; futures returned by ``Batcher.post`` resolve to the
natural result value (int for register reads, None otherwise).

The DP register addresses below are byte offsets; the upper nibble of
``addr`` selects the DP-side bank (DPBANKSEL). Subclasses translate
``ApRead/ApWrite`` and ``DpRead/DpWrite`` to wire-protocol shifts and
manage SELECT/SELECT1 caching.

Subclass contract:

* Override ``flush_ops`` to lower batched ops to the wire layer.
* Resolve each user-facing future with the natural result value (int
  for ``ApRead``/``DpRead``; ``None`` otherwise).
* Treat AP-read pipelining as part of the lowering: AP reads complete
  via the *next* shift's response (or a forced RDBUFF flush at end of
  batch).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ...engine import Batcher
from ...node import Node
from ...part_id import PartId


# --- Op dataclasses (frozen, inputs only) --------------------------

@dataclass(frozen=True, slots=True)
class ApRead:
    """Read a 32-bit register at an absolute system address.

    ADIv6 model carried through to ADIv5: in ADIv6 the DP exposes a
    flat system address space and APs are 4 KB regions inside it;
    each `ApRead(addr)` is one APACC at that address. ADIv5's
    APSEL+APBANKSEL+register-offset retrofits onto the same model:
    callers compose ``(apsel << 24) | reg_offset``, which is exactly
    what an ADIv5 SELECT register needs to load. The DP's
    ``_select_for`` then extracts the right SELECT view per protocol
    (legacy APSEL/APBANKSEL/DPBANKSEL split, or ADIv6 unified
    ADDR[31:4])."""
    addr: int

    def __repr__(self):
        return f"ApRead({self.addr:#x})"

@dataclass(frozen=True, slots=True)
class ApWrite:
    addr: int
    data: int

    def __repr__(self):
        return f"ApWrite({self.addr:#x}, {self.data:#010x})"

@dataclass(frozen=True, slots=True)
class DpRead:
    """Read DP register. ``addr`` is byte offset; upper nibble is
    DPBANKSEL (which is loaded into SELECT before access)."""
    addr: int

    def __repr__(self):
        return f"DpRead({self.addr:#x})"

@dataclass(frozen=True, slots=True)
class DpWrite:
    addr: int
    data: int

    def __repr__(self):
        return f"DpWrite({self.addr:#x}, {self.data:#010x})"

@dataclass(frozen=True, slots=True)
class Abort:
    """Issue ABORT (clear sticky flags). On JTAG, lowered via the
    dedicated ABORT IR; on SWD, lowered as a write to DP addr 0x00."""
    what: int = 0x1f

    def __repr__(self):
        return f"Abort({self.what:#x})"

@dataclass(frozen=True, slots=True)
class Run:
    """Idle clock cycles between transactions."""
    cycles: int

    def __repr__(self):
        return f"Run({self.cycles})"

@dataclass(frozen=True, slots=True)
class ChipId:
    """Best-available identifier for the chip behind a DP.

    Composes a :class:`PartId` (the JEP106 + part + revision payload
    that's also used for component PIDR / TAP IDCODE) with a
    ``source`` tag recording where the value came from. TARGETID is
    designed for this and considered authoritative; ROM Table PIDR
    can be a debug-fabric IP block that's shared across multiple
    chips, so it's a coarser identifier."""
    partid: PartId
    source: str  # "TARGETID" | "ROMTABLE@<addr>" | ...

    # Pass-through accessors for ergonomic field access.
    @property
    def jep106_bank(self) -> int:
        return self.partid.jep106_bank

    @property
    def jep106_id(self) -> int:
        return self.partid.jep106_id

    @property
    def part_no(self) -> int:
        return self.partid.part_no

    @property
    def revision(self) -> int:
        return self.partid.revision

    @property
    def manufacturer_name(self) -> str:
        return self.partid.manufacturer_name

    def __str__(self):
        return f"{self.partid.pretty()} via {self.source}"


# --- Errors --------------------------------------------------------

class DpAccessFailure(Exception):
    """DP/AP access failed (bad ACK, sticky error, parity, etc.)."""


# --- ADIv6 system-address bus adapter -----------------------------

class DpSystemBus:
    """Bus adapter exposing ``read32``/``write32`` at absolute system
    addresses by issuing direct APACC ops on a :class:`Dp`. Used as
    the bus argument for ADIv6 top-level component discovery
    (BASEPTR0 walk) and for any non-MEM-AP component reachable via
    direct register access in the DP's system address space.

    Distinct from a MEM-AP: there is no CSW/TAR/DRW state machine —
    each access is one APACC at SELECT composed from ``addr[31:4]``.
    The DP's system address space is the debug fabric, directly
    addressable through APACC. Only 32-bit aligned word accesses are
    supported (component management registers and ROM Table entries
    are word-aligned)."""

    def __init__(self, dp: "Dp"):
        self._dp = dp
        # Mirror DP's logger so child components attached via this bus
        # surface log lines under the DP's tree path.
        self.logger = dp.logger
        # System-address size in bits (DPIDR1.ASIZE). Used by the
        # ROM Table walker to mask computed child addresses — some
        # chips leave stale non-zero bits above ASIZE in the
        # OFFSET[63:32] half of 64-bit ROM entries (spec says RES0,
        # but wire-level access silently truncates).
        self.addr_size_bits: int = dp.asize if dp.asize else 32

    def read32(self, addr: int):
        if addr & 0x3:
            raise ValueError(
                f"DpSystemBus.read32: unaligned address 0x{addr:x}")
        return self._dp.post(ApRead(addr=addr))

    def write32(self, addr: int, data: int):
        if addr & 0x3:
            raise ValueError(
                f"DpSystemBus.write32: unaligned address 0x{addr:x}")
        return self._dp.post(ApWrite(addr=addr, data=data & 0xffffffff))


# --- Abstract DP ---------------------------------------------------

class Dp(Batcher, Node):
    """Abstract ARM Debug Port. Subclasses (JtagDp, SwDp) provide the
    wire-specific lowering in ``flush_ops``."""

    # DP register byte offsets; (offset & 0xf0) selects DPBANKSEL.
    DPIDR     = 0x00  # bank 0, R; W -> ABORT (SWD)
    CTRL_STAT = 0x04  # bank 0
    DLCR      = 0x14  # bank 1 (ADIv5)
    DPIDR1    = 0x10  # bank 1 (ADIv6 / DPv3+)
    BASEPTR0  = 0x20  # bank 2 (ADIv6)
    TARGETID  = 0x24  # bank 2
    BASEPTR1  = 0x30  # bank 3 (ADIv6)
    DLPIDR    = 0x34  # bank 3
    EVENTSTAT = 0x44  # bank 4
    SELECT1   = 0x54  # bank 5 (ADIv6, W)
    TARGETID1 = 0x64  # bank 6 (ADIv6)
    SELECT    = 0x08  # bank-agnostic, W
    RDBUFF    = 0x0c  # bank-agnostic, R
    TARGETSEL = 0x0c  # bank-agnostic, W (multidrop)

    # CTRL/STAT bits
    ORUNDETECT   = 1 << 0
    STICKYORUN   = 1 << 1
    STICKYCMP    = 1 << 4
    STICKYERR    = 1 << 5
    READOK       = 1 << 6
    WDATAERR     = 1 << 7
    CDBGRSTREQ   = 1 << 26
    CDBGRSTACK   = 1 << 27
    CDBGPWRUPREQ = 1 << 28
    CDBGPWRUPACK = 1 << 29
    CSYSPWRUPREQ = 1 << 30
    CSYSPWRUPACK = 1 << 31

    PWRUP_ACK_MASK = CDBGPWRUPACK | CSYSPWRUPACK
    PWRUP_REQ_MASK = CDBGPWRUPREQ | CSYSPWRUPREQ

    # ABORT register bits.
    DAPABORT     = 1 << 0  # cancel current AP transaction
    STKCMPCLR    = 1 << 1  # clear CTRL/STAT.STICKYCMP
    STKERRCLR    = 1 << 2  # clear CTRL/STAT.STICKYERR
    WDERRCLR     = 1 << 3  # clear CTRL/STAT.WDATAERR
    ORUNERRCLR   = 1 << 4  # clear CTRL/STAT.STICKYORUN
    ABORT_ALL    = (DAPABORT | STKCMPCLR | STKERRCLR
                    | WDERRCLR | ORUNERRCLR)

    def __init__(self, name: str = "dap"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.dpidr: int | None = None
        self.dpidr1: int | None = None
        self.dp_version: int | None = None  # DPVER (0=DPv0, 1=DPv1, 2=DPv2, 3=DPv3=ADIv6)
        self.adi_version: int | None = None  # 5 or 6
        self.targetid: int | None = None     # DPv2+: chip designer/part/revision
        self.targetid1: int | None = None    # ADIv6: vendor-defined extension
        self.asize: int | None = None        # ADIv6 system-address size in bits
                                             # (DPIDR1.ASIZE; None on ADIv5)

    async def start(self):
        """Read DPIDR, clear sticky flags, power up debug+system domains."""
        self.dpidr = await self.post(DpRead(self.DPIDR))
        self.dp_version = (self.dpidr >> 12) & 0xf
        self.adi_version = 6 if self.dp_version >= 3 else 5
        self.logger.info(
            "DPIDR 0x%08x — DPv%d (ADIv%d)",
            self.dpidr, self.dp_version, self.adi_version)

        if self.adi_version == 6:
            self.dpidr1 = await self.post(DpRead(self.DPIDR1))
            # DPIDR1[6:0] = ASIZE (system-address size in bits).
            self.asize = self.dpidr1 & 0x7f
            self.logger.info(
                "DPIDR1 0x%08x — ASIZE=%d", self.dpidr1, self.asize)

        # TARGETID identifies the chip (designer/part/revision), as
        # opposed to the DP IP itself (DPIDR). Available on DPv2+.
        # ADIv6 adds TARGETID1 for vendor-defined extensions.
        if self.dp_version >= 2:
            self.targetid = await self.post(DpRead(self.TARGETID))
            if self.targetid & 0x1:
                self.logger.info(
                    "TARGETID 0x%08x — %s",
                    self.targetid,
                    PartId.from_idcode(self.targetid).pretty())
            else:
                self.logger.info(
                    "TARGETID 0x%08x — bit[0] not set, "
                    "manufacturer didn't populate the register",
                    self.targetid)
            if self.adi_version == 6:
                self.targetid1 = await self.post(DpRead(self.TARGETID1))
                self.logger.info("TARGETID1 0x%08x", self.targetid1)
        else:
            self.logger.info(
                "TARGETID: not available (DPv%d, requires DPv2+)",
                self.dp_version)

        # ABORT register layout (NOT the CTRL/STAT layout):
        #   bit 0: DAPABORT, 1: STKCMPCLR, 2: STKERRCLR,
        #   3: WDERRCLR,    4: ORUNERRCLR.
        await self.post(Abort(self.ABORT_ALL))
        await self.post(DpWrite(self.CTRL_STAT, self.PWRUP_REQ_MASK))

        for _ in range(50):
            stat = await self.post(DpRead(self.CTRL_STAT))
            if (stat & self.PWRUP_ACK_MASK) == self.PWRUP_ACK_MASK:
                self.logger.info("DP powered up (CTRL/STAT 0x%08x)", stat)
                break
            await asyncio.sleep(0.005)
        else:
            raise DpAccessFailure(
                f"DP power-up timeout (CTRL/STAT 0x{stat:08x})")

        await self._enumerate_aps()

    # AP indices to probe on DPv0-v2. Crobe scans 0..15 + 240..255;
    # APs at indices 16..239 are exceptionally rare. Cheap to widen
    # if a real chip surfaces.
    AP_PROBE_INDICES = list(range(16)) + list(range(240, 256))

    async def _enumerate_aps(self):
        """Discover APs and add them as children. DPv0-v2 walks
        APSEL at fixed indices; ADIv6 walks the system-address ROM
        Table rooted at BASEPTR0.

        Per-APSEL discovery failures are logged and the walk
        continues. Per-AP ``start()`` failures are isolated by the
        :meth:`start_tree` override below."""
        if self.adi_version >= 6:
            await self._enumerate_aps_adiv6()
            return

        # Imported lazily to avoid a circular dependency at module-load.
        from .ap import Ap

        # DPv0-v2: walk APSEL space, batch all the IDR reads at once.
        futures = [
            (apsel, Ap.discover(self, base=apsel << 24))
            for apsel in self.AP_PROBE_INDICES
        ]
        for apsel, coro in futures:
            try:
                ap = await coro
            except Exception as exc:
                self.logger.warning(
                    "AP discovery at APSEL %d crashed: %s",
                    apsel, exc, exc_info=True)
                continue
            if ap is not None:
                self.child_add(ap)
                self.logger.info(
                    "AP%d discovered: idr=0x%08x class=0x%x type=0x%x",
                    apsel, ap.idr, ap.klass, ap.type)

    async def _enumerate_aps_adiv6(self):
        """ADIv6 path: read BASEPTR0 (and BASEPTR1 if ASIZE > 32),
        discover the top-level component at that system address, and
        attach it as a child. Typically a ROM Table whose entries
        point to APs and other CoreSight components in the DP's
        system address space."""
        from .coresight.model import MemoryMappedComponent
        from .coresight.power_gate import FailureKind, PowerGate

        # DPIDR1.ASIZE: bits[6:0] — system address size in bits.
        asize = (self.dpidr1 or 0) & 0x7f
        if asize > 32:
            # SELECT1 (ADDR[63:32]) needed for >32-bit address space.
            # Not yet wired in JtagDp/SwDp — components above the
            # 4 GiB boundary will fault.
            self.logger.warning(
                "DPIDR1.ASIZE=%d > 32: SELECT1 unsupported, only "
                "ADDR[31:0] reachable", asize)

        baseptr0 = await self.post(DpRead(self.BASEPTR0))
        self.logger.info("BASEPTR0 0x%08x", baseptr0)
        if not (baseptr0 & 0x1):
            self.logger.warning(
                "BASEPTR0.VALID=0 — no top-level component, "
                "no APs to enumerate")
            return
        base_addr = baseptr0 & 0xfffff000

        bus = DpSystemBus(self)
        try:
            comp = await MemoryMappedComponent.discover(bus, base_addr)
        except Exception as exc:
            self.logger.warning(
                "Top-level component at 0x%x: discover failed: %s",
                base_addr, exc, exc_info=True)
            comp = PowerGate(bus, base_addr, FailureKind.FAULT)

        if (isinstance(comp, MemoryMappedComponent)
                and comp.cidr_class is None):
            self.logger.warning(
                "Top-level component at 0x%x: no CIDR preamble — "
                "installing PowerGate", base_addr)
            comp = PowerGate(bus, base_addr, FailureKind.EMPTY)

        self.child_add(comp)

    async def start_tree(self):
        """Best-effort tree start: a single AP's failed ``start()``
        (e.g. a chip whose CFG/BASE is unresponsive on one AP) is
        logged but doesn't drop sibling APs from the tree. The failed
        AP remains attached, just with an incomplete subtree.

        After all children have been started (or failed), log the
        chip identifier — by the time AP root ROM Tables are
        discovered, ``chip_id()``'s fallback path is ready."""
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
        chip = self.chip_id()
        if chip is not None:
            self.logger.info("Chip ID: %s", chip)
        else:
            self.logger.info("Chip ID: unidentified")

    async def flush_ops(self, batch):
        raise NotImplementedError(
            f"{type(self).__name__} must implement flush_ops")

    # IDR.TYPE values whose connected bus is conventionally main
    # system memory rather than the debug fabric. Used by
    # :meth:`system_memap` to rank candidate MEM-APs.
    _SYSTEM_BUS_TYPES = frozenset({
        0x1,  # AHB-AP
        0x4,  # AXI-AP
        0x5,  # AHB5-AP
        0x7,  # AXI5-AP
        0x8,  # AHB5-AP with enhanced HPROT
    })

    def system_memap(self):
        """Return the MEM-AP most likely to bridge to main system
        memory, or ``None`` if no MEM-AP is discovered.

        ADI does not flag any AP as "the system memory one" — chip
        designers wire APs as they please. This method applies a
        heuristic that gets the right answer on conventional Cortex-A
        / Cortex-M layouts; precise routing for a specific SoC
        belongs in a target-framework driver. The ranking is:

          1. ``IDR.TYPE`` in ``_SYSTEM_BUS_TYPES`` (AHB / AHB5 /
             AHB5-EnH / AXI / AXI5) is preferred over APB / APB4
             (debug fabric).
          2. ``BASE.P == 0`` (no embedded debug ROM Table inside the
             AP's aperture) is preferred over ``BASE.P == 1``: a
             MEM-AP with a ROM Table inside is by convention
             carrying debug components, not RAM.

        Among ties, the first match in tree-walk order wins.

        Both ADIv5 (``MemAp`` directly under the DP) and ADIv6
        (``MemApV2`` under the top-level system-bus ROM Table) layouts
        are covered by walking the DP's whole subtree."""
        # Lazy-import MemAp / MemApV2 to avoid a module-load circular
        # dependency through coresight.model.
        from .mem_ap import MemAp, MemApV2

        candidates = (self.children_of_class(MemAp)
                      + self.children_of_class(MemApV2))
        if not candidates:
            return None

        def rank(ap):
            type_field = (ap.idr or 0) & 0xf
            is_system_type = type_field in self._SYSTEM_BUS_TYPES
            has_debug_rom = ap.base_addr is not None
            # Lower tuple sorts first.
            return (0 if is_system_type else 1,
                    1 if has_debug_rom else 0)

        candidates.sort(key=rank)
        return candidates[0]

    def chip_id(self) -> "ChipId | None":
        """Return the best-available chip identifier, in this order
        of preference:

        1. **TARGETID** (DPv2+) when populated. TARGETID's bit[0] is
           RES1 in spec, so a value with bit 0 cleared indicates the
           manufacturer didn't populate it — fall through.
        2. **Root ROM Table PIDR**. The topmost ROM Table reachable
           from the DP is the legacy place where chip identity has
           lived since ADIv5.0 and is still where some manufacturers
           keep it even on DPv2+ hardware. The DP's subtree shape
           differs by ADI version — on ADIv5 the ROM Table sits
           under a MEM-AP child of the DP, on ADIv6 it sits directly
           under the DP via the BASEPTR0 walk — so we let
           ``children_of_class`` find it regardless.

        Returns ``None`` when neither source is available — typical
        only on chips that fail discovery entirely or have an empty
        BASE on every AP."""
        # 1. TARGETID — only meaningful when bit[0] (RES1) is actually 1.
        # The TARGETID layout matches a JTAG IDCODE; PartId.from_idcode
        # parses it directly.
        if self.targetid is not None and (self.targetid & 0x1):
            return ChipId(
                partid=PartId.from_idcode(self.targetid),
                source="TARGETID",
            )

        # 2. First ROM Table in the DP's subtree. ``children_of_class``
        # walks pre-order DFS, so the topmost ROM Table comes first —
        # matches what "root ROM Table" means on either ADI version:
        # the AP-mediated ROM on ADIv5, the BASEPTR0 ROM on ADIv6.
        # Lazy import avoids a module-load circular dep.
        from .coresight.rom_table import RomTable

        for rt in self.children_of_class(RomTable):
            if rt.cidr_class is None:
                continue
            return ChipId(
                partid=rt.partid,
                source=f"ROMTABLE@0x{rt.base:x}",
            )

        return None

    def __repr__(self):
        return f"<{type(self).__name__} {self._name}>"
