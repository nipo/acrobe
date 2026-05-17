"""CoreSight component model: PIDR/CIDR/DEVARCH parsing and the
three classification registries.

A debug component that lives in some memory map (the `bus` argument,
typically a MEM-AP) is identified by a stack of standard register
banks at +0xFB0..+0xFFC of its base address:

* CIDR0..3 — preamble (0x0D / 0x_0 / 0x05 / 0xB1) + 4-bit component
  class.
* PIDR0..7 — a JEP106 designer code, a 12-bit part number, plus
  revision/customer-mod/RevAnd metadata, plus a SIZE field giving
  the component's footprint in 4 KB units.
* DEVARCH (class-0x9 only) — 11-bit architecture-designer JEP106 +
  16-bit ARCHID identifying the architecture (e.g. ETMv4 = 0x4A13).
* DEVTYPE (class-0x9 only) — coarse-grained MAJOR/SUB type used
  before DEVARCH was widely adopted; still the only ID on legacy
  v0/v1 CoreSight components.

Three registries (in lookup precedence) drive subclass selection
in :meth:`MemoryMappedComponent.discover`:

1. **`MemoryMappedComponent.devarch_db`** — keyed on `DevArch`,
   matching ARCHITECT + ARCHID with REVISION masked. The right
   choice for ADIv6 / ARMv8 components.

2. **`MemoryMappedComponent.db`** — keyed on `PartId`. PartId
   excludes the revision field (PIDR2[7:4]) so a single
   registration covers all silicon revisions of a part.

3. **`CoresightComponent.db`** — keyed on the 8-bit DEVTYPE
   (class-0x9 only). The legacy lane.

Per-SoC overrides keyed on (PartId, address) live on
`RomTable.soc_db` — added in slice 5 alongside the ROM walker.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ....db import Db, NoMatch
from ....node import Node
from ....part_id import PartId  # noqa: F401 — re-exported for convenience


def _devarch_eq(key: "DevArch", lookup: "DevArch") -> bool:
    """DEVARCH equality — match ARCHITECT + ARCHID, ignore REVISION,
    require both to have PRESENT=1."""
    if not key.present or not lookup.present:
        return False
    return key.architect == lookup.architect and key.archid == lookup.archid


@dataclass(frozen=True, slots=True)
class DevArch:
    """Decoded DEVARCH register (class-0x9 only).

    Hash/equality include REVISION + present so test fixtures can
    construct distinct values; lookup via `devarch_db` uses
    :func:`_devarch_eq` which masks REVISION."""

    architect: int   # 11-bit JEP106 of architecture designer
    archid: int      # 16-bit architecture ID
    revision: int    # 4-bit architecture revision
    present: bool    # bit[20]; if 0, the rest is RAZ


@dataclass(frozen=True, slots=True)
class ComponentIds:
    """All identification data extracted by reading PIDR/CIDR/DEVARCH/
    DEVTYPE/DEVID at a component's base address.

    `partid.revision` carries PIDR2[7:4]; the `cmod` / `rev_and`
    sibling fields hold the PIDR3 bytes that aren't part of the
    canonical IDCODE-equivalent identity.

    `cidr_class` is None when the CIDR preamble doesn't match the
    expected (0x0D, 0x05, 0xB1) — i.e. there's no CoreSight component
    here (read garbage, power-gated, or the address doesn't host one)."""

    cidr_class: int | None
    partid: PartId    # JEP106 + part_no + revision (PIDR2[7:4])
    cmod: int         # PIDR3[3:0]
    rev_and: int      # PIDR3[7:4]
    size_log2: int    # PIDR4[7:4] — component spans 2^size 4 KB blocks
    devarch: DevArch | None
    devtype: int | None    # 8-bit DEVTYPE (None for non-class-0x9)
    devid: int | None      # 32-bit DEVID

    @classmethod
    async def read(cls, bus, base: int) -> "ComponentIds":
        """Batch-read PIDR0..7, CIDR0..3, DEVARCH/DEVID/DEVTYPE from
        a component at ``base`` on ``bus``. ``bus`` must expose
        ``read32(addr) -> Future[int]`` (e.g. a MemAp)."""
        addrs = (
            MemoryMappedComponent.PIDR4,
            MemoryMappedComponent.PIDR5,
            MemoryMappedComponent.PIDR6,
            MemoryMappedComponent.PIDR7,
            MemoryMappedComponent.PIDR0,
            MemoryMappedComponent.PIDR1,
            MemoryMappedComponent.PIDR2,
            MemoryMappedComponent.PIDR3,
            MemoryMappedComponent.CIDR0,
            MemoryMappedComponent.CIDR1,
            MemoryMappedComponent.CIDR2,
            MemoryMappedComponent.CIDR3,
            MemoryMappedComponent.DEVARCH,
            MemoryMappedComponent.DEVID,
            MemoryMappedComponent.DEVTYPE,
        )

        value_fut = {a: bus.read32(base + a) for a in sorted(addrs)}
        p0 = await value_fut[MemoryMappedComponent.PIDR0]
        p1 = await value_fut[MemoryMappedComponent.PIDR1]
        p2 = await value_fut[MemoryMappedComponent.PIDR2]
        p3 = await value_fut[MemoryMappedComponent.PIDR3]
        p4 = await value_fut[MemoryMappedComponent.PIDR4]
        _p5 = await value_fut[MemoryMappedComponent.PIDR5]
        _p6 = await value_fut[MemoryMappedComponent.PIDR6]
        _p7 = await value_fut[MemoryMappedComponent.PIDR7]
        c0 = await value_fut[MemoryMappedComponent.CIDR0]
        c1 = await value_fut[MemoryMappedComponent.CIDR1]
        c2 = await value_fut[MemoryMappedComponent.CIDR2]
        c3 = await value_fut[MemoryMappedComponent.CIDR3]
        devarch_raw = await value_fut[MemoryMappedComponent.DEVARCH]
        devid_raw = await value_fut[MemoryMappedComponent.DEVID]
        devtype_raw = await value_fut[MemoryMappedComponent.DEVTYPE]

        # Validate CIDR preamble: 0xB1_05_xx_0D spread across the
        # low byte of each word. PRMBL_1 (CIDR1[3:0]) is always 0.
        c0_lo = c0 & 0xFF
        c1_lo = c1 & 0xFF
        c2_lo = c2 & 0xFF
        c3_lo = c3 & 0xFF
        if c0_lo != 0x0D or c2_lo != 0x05 or c3_lo != 0xB1 or (c1_lo & 0xF) != 0x0:
            return cls.empty()

        cidr_class = (c1_lo >> 4) & 0xF

        partid = PartId(
            jep106_bank=p4 & 0xF,
            jep106_id=((p2 & 0x7) << 4) | ((p1 >> 4) & 0xF),
            part_no=((p1 & 0xF) << 8) | (p0 & 0xFF),
            revision=(p2 >> 4) & 0xF,
        )
        cmod = p3 & 0xF
        rev_and = (p3 >> 4) & 0xF
        size_log2 = (p4 >> 4) & 0xF

        if cidr_class == MemoryMappedComponent.CLASS_CORESIGHT:
            devarch = DevArch(
                architect=(devarch_raw >> 21) & 0x7FF,
                archid=devarch_raw & 0xFFFF,
                revision=(devarch_raw >> 16) & 0xF,
                present=bool((devarch_raw >> 20) & 1),
            )
            devtype = devtype_raw & 0xFF
            devid = devid_raw & 0xFFFFFFFF
        else:
            devarch = None
            devtype = None
            devid = None

        return cls(
            cidr_class=cidr_class,
            partid=partid,
            cmod=cmod,
            rev_and=rev_and,
            size_log2=size_log2,
            devarch=devarch,
            devtype=devtype,
            devid=devid,
        )

    @classmethod
    def empty(cls) -> "ComponentIds":
        """Return the metadata sentinel for "no component here"
        (preamble mismatch, all-zero reads, etc.)."""
        return cls(
            cidr_class=None,
            partid=PartId(0, 0, 0, 0),
            cmod=0, rev_and=0, size_log2=0,
            devarch=None, devtype=None, devid=None,
        )


# --- Memory-mapped component ---------------------------------------

class MemoryMappedComponent(Node):
    """A debug component identified by its CoreSight ID registers.

    Subclasses register against one of:

    * :data:`devarch_db` — keyed on :class:`DevArch` (class-0x9 ADIv6).
    * :data:`db` — keyed on :class:`PartId` (any class).
    * :data:`CoresightComponent.db` — keyed on DEVTYPE (class-0x9 legacy).

    :meth:`discover` reads the ID banks and instantiates the
    most-specific registered class, falling back to the base class
    when nothing matches (so unknown components still show up in
    enumeration with their JEP106 + part number)."""

    # Standard CoreSight management register offsets.
    DEVARCH = 0xFBC
    DEVID   = 0xFC8
    DEVTYPE = 0xFCC
    PIDR4   = 0xFD0
    PIDR5   = 0xFD4
    PIDR6   = 0xFD8
    PIDR7   = 0xFDC
    PIDR0   = 0xFE0
    PIDR1   = 0xFE4
    PIDR2   = 0xFE8
    PIDR3   = 0xFEC
    CIDR0   = 0xFF0
    CIDR1   = 0xFF4
    CIDR2   = 0xFF8
    CIDR3   = 0xFFC

    # Component class values from CIDR1[7:4].
    CLASS_GENERIC      = 0x0
    CLASS_ROM_TABLE    = 0x1
    CLASS_CORESIGHT    = 0x9
    CLASS_PERIPHERAL   = 0xB
    CLASS_GENERIC_IP   = 0xE
    CLASS_PRIMECELL    = 0xF

    db: Db = Db("CoreSight PartId",
                eq_func=lambda key, lookup: key.is_same_part(lookup))
    devarch_db: Db = Db("CoreSight DEVARCH", eq_func=_devarch_eq)

    # Subclasses set this to a human-readable component name (e.g.
    # "Trace Port Interface Unit"); the default name becomes
    # "<FRIENDLY_NAME>@<base:08x>". When empty, the generic
    # archid/part fallback is used.
    FRIENDLY_NAME: str = ""

    def __init__(self, bus, base: int, ids: ComponentIds,
                 name: str | None = None):
        if name is None:
            name = self.__default_name(ids, base)
        super().__init__(name)
        self._bus = bus
        self.base = base
        self.ids = ids

    # -- Convenience accessors over the ID bundle -------------------

    @property
    def cidr_class(self) -> int | None:
        return self.ids.cidr_class

    @property
    def partid(self) -> PartId:
        return self.ids.partid

    @property
    def revision(self) -> int:
        return self.ids.partid.revision

    @property
    def devarch(self) -> DevArch | None:
        return self.ids.devarch

    @property
    def devtype(self) -> int | None:
        return self.ids.devtype

    @property
    def devid(self) -> int | None:
        return self.ids.devid

    @property
    def size_bytes(self) -> int:
        """Component footprint in bytes, derived from PIDR4.SIZE."""
        return 0x1000 << self.ids.size_log2

    # -- Register access ------------------------------------------

    def reg_read(self, offset: int):
        """Read the 32-bit register at ``self.base + offset``. Returns
        a Future that resolves with the register value. Sugar over
        ``self._bus.read32`` for the common case where a component
        accesses its own aperture; ``self._bus`` is still the right
        path for absolute or out-of-aperture addresses."""
        return self._bus.read32(self.base + offset)

    def reg_write(self, offset: int, data: int):
        """Write a 32-bit register at ``self.base + offset``. Returns
        a Future that resolves once the write commits."""
        return self._bus.write32(self.base + offset, data)

    # -- Friendly default naming ------------------------------------

    def __default_name(self, ids: ComponentIds, base: int) -> str:
        if self.FRIENDLY_NAME:
            return f"{self.FRIENDLY_NAME}@{base:08x}"
        if ids.cidr_class is None:
            return f"unknown@{base:08x}"
        designer = ids.partid.manufacturer_name
        if ids.devarch is not None and ids.devarch.present:
            return (f"comp@{base:08x}"
                    f"[archid={ids.devarch.archid:#06x}, {designer}]")
        return (f"comp@{base:08x}"
                f"[part={ids.partid.part_no:#05x}, {designer}]")

    # -- Discovery (the canonical entry point) ----------------------

    @classmethod
    async def discover(cls, bus, base: int) -> "MemoryMappedComponent":
        """Read the ID banks at ``base`` and instantiate the
        most-specific registered subclass. Falls back to a base
        :class:`MemoryMappedComponent` when no registry hits — useful
        for surfacing unknown components in enumeration."""
        ids = await ComponentIds.read(bus, base)
        chosen = _pick_class(ids)
        return chosen(bus, base, ids)

    async def start(self):
        """Default: nothing. Subclasses override to fetch additional
        registers, configure the component, etc."""
        pass

    async def start_tree(self):
        """Best-effort tree start. A single child's failed start()
        is logged with a traceback but does not drop sibling children
        or block their start. Matches the ARM-debug discovery
        philosophy: surface as much of the tree as possible, even
        when individual subtrees are unreachable."""
        await self._ensure_started()
        for child in self._children:
            try:
                await child.start_tree()
            except Exception as exc:
                self.logger.warning(
                    "Child %r start failed: %s. Subtree incomplete.",
                    child.name, exc, exc_info=True)

    def __repr__(self):
        ids = self.ids
        return (f"<{type(self).__name__} {self._name} "
                f"class={ids.cidr_class} {ids.partid}>")


class CoresightComponent(MemoryMappedComponent):
    """Class-0x9 CoreSight component. Subclasses that key on DEVTYPE
    (the legacy classification) register against :data:`db`."""

    db: Db = Db("CoreSight DEVTYPE")


def _pick_class(ids: ComponentIds):
    """Lookup precedence: DEVARCH → CIDR class 0x1 (ROM Table) →
    PartId → DEVTYPE → fallback. Return the class to instantiate.

    CIDR class 0x1 outranks the PartId registry because ROM Tables
    don't carry a vendor-distinct PartId — many SoCs ship multiple
    ROM Tables that all share whatever PartId the implementer
    happened to leave in PIDR. A PartId-keyed handler with class 0x1
    in the wild would almost certainly mean we mistook a ROM Table
    for a leaf component."""
    # 1. DEVARCH-based registry (class 0x9, PRESENT=1).
    if ids.devarch is not None and ids.devarch.present:
        try:
            handlers = MemoryMappedComponent.devarch_db.get(
                ids.devarch, allow_default=False)
            return handlers[0]
        except NoMatch:
            pass
    # 2. Class 0x1 ROM Tables — CIDR class is the authoritative
    #    type indicator. Lazy import to avoid circular dep.
    if ids.cidr_class == MemoryMappedComponent.CLASS_ROM_TABLE:
        from .rom_table import RomTable
        return RomTable
    # 3. PartId-based registry. Skip when there's no valid component.
    if ids.cidr_class is not None:
        try:
            handlers = MemoryMappedComponent.db.get(
                ids.partid, allow_default=False)
            return handlers[0]
        except NoMatch:
            pass
    # 4. DEVTYPE registry (class 0x9 only).
    if ids.cidr_class == MemoryMappedComponent.CLASS_CORESIGHT \
            and ids.devtype is not None:
        try:
            handlers = CoresightComponent.db.get(
                ids.devtype, allow_default=False)
            return handlers[0]
        except NoMatch:
            pass
    # 5. Fallback to the generic class.
    return MemoryMappedComponent
