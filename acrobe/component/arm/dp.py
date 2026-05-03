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

@dataclass(frozen=True)
class ApRead:
    """Read AP register. ``ap`` is the AP base address (ADIv6 view;
    ADIv5 indices fall on ``index << 24`` boundaries). ``addr`` is the
    AP register byte offset; upper nibble selects APBANKSEL."""
    ap: int
    addr: int


@dataclass(frozen=True)
class ApWrite:
    ap: int
    addr: int
    data: int


@dataclass(frozen=True)
class DpRead:
    """Read DP register. ``addr`` is byte offset; upper nibble is
    DPBANKSEL (which is loaded into SELECT before access)."""
    addr: int


@dataclass(frozen=True)
class DpWrite:
    addr: int
    data: int


@dataclass(frozen=True)
class Abort:
    """Issue ABORT (clear sticky flags). On JTAG, lowered via the
    dedicated ABORT IR; on SWD, lowered as a write to DP addr 0x00."""
    what: int = 0x1f


@dataclass(frozen=True)
class Run:
    """Idle clock cycles between transactions."""
    cycles: int


@dataclass(frozen=True)
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

    def __init__(self, name: str = "dap"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.dpidr: int | None = None
        self.dpidr1: int | None = None
        self.dp_version: int | None = None  # DPVER (0=DPv0, 1=DPv1, 2=DPv2, 3=DPv3=ADIv6)
        self.adi_version: int | None = None  # 5 or 6
        self.targetid: int | None = None     # DPv2+: chip designer/part/revision
        self.targetid1: int | None = None    # ADIv6: vendor-defined extension

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
            self.logger.info("DPIDR1 0x%08x", self.dpidr1)

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

        await self.post(Abort(self.STICKYORUN | self.STICKYCMP
                              | self.STICKYERR | self.WDATAERR | 0x1))
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
        APSEL at fixed indices; DPv3 (ADIv6) walks the ROM Table at
        BASEPTR0/1 — implemented in slice 5 alongside the general
        CoreSight ROM walker.

        Per-APSEL discovery failures are logged and the walk
        continues. Per-AP ``start()`` failures are isolated by the
        :meth:`start_tree` override below."""
        # Imported lazily to avoid a circular dependency at module-load.
        from .ap import Ap

        if self.adi_version >= 6:
            self.logger.warning(
                "ADIv6 (DPv3) AP enumeration via BASEPTR is not yet "
                "implemented — slice 5 will land it. No APs added.")
            return

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

    def chip_id(self) -> "ChipId | None":
        """Return the best-available chip identifier, in this order
        of preference:

        1. **TARGETID** (DPv2+) when populated. TARGETID's bit[0] is
           RES1 in spec, so a value with bit 0 cleared indicates the
           manufacturer didn't populate it — fall through.
        2. **Root ROM Table PIDR** discovered under any MEM-AP child.
           This is the legacy place where chip identity has lived
           since ADIv5.0 and is still where some manufacturers keep
           it even on DPv2+ hardware.

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

        # 2. Root ROM Table PIDR. Look at each AP child and its first
        # discovered child (typically the ROM Table at AP.BASE).
        # Lazy imports avoid a circular dependency at module-load.
        from .ap import Ap
        from .coresight.model import MemoryMappedComponent

        for ap in self._children:
            if not isinstance(ap, Ap):
                continue
            for grandchild in ap._children:
                if not isinstance(grandchild, MemoryMappedComponent):
                    continue
                if grandchild.cidr_class is None:
                    continue
                return ChipId(
                    partid=grandchild.partid,
                    source=f"ROMTABLE@0x{grandchild.base:x}",
                )

        return None

    def __repr__(self):
        return f"<{type(self).__name__} {self._name}>"
