"""CoreSight ROM Table walker.

A ROM Table is itself a CoreSight-tagged component (Class 0x1 in the
ADIv5 view, Class 0x9 with DEVARCH ARCHID = 0x0AF7 in the ADIv6
view). Its body holds a list of entries, each pointing to another
component (or another ROM Table — nesting is unbounded).

This walker is **passive**: it never writes anything (no power-up, no
unlock, no `enable` bits). When an entry's child can't be classified
(read fault, missing CIDR preamble), a :class:`PowerGate` is
installed in its place — the Target framework can later call
``gate.retry()`` after explicitly powering up the relevant domain.

Per-SoC overrides on :attr:`RomTable.soc_db` are keyed on
``(parent_rom_partid, child_address)`` and supersede the standard
DEVARCH/PartId/DEVTYPE precedence — useful when a chip places a
generic CoreSight component at a known address that the
device-agnostic registries can't disambiguate (canonical example:
the Cortex-M SCS at 0xE000_E000 reachable from a generic ROM Table)."""

from __future__ import annotations

from ....db import Db, NoMatch
from ..dp import DpAccessFailure
from .model import (
    ComponentIds, CoresightComponent, DevArch,
    MemoryMappedComponent, PartId, _pick_class,
)
from .power_gate import FailureKind, PowerGate


# DEVID.FORMAT for Class 0x9 ROM Tables (IHI0074F D3.5.11.1) lives in
# bits[3:0]; FORMAT==0x0 means 32-bit ROMENTRY format and FORMAT==0x1
# means 64-bit. Bit[4] is SYSMEM (deprecated) and was previously
# misread as FORMAT — a chip with SYSMEM=1 (Agilex 5 HPS) was decoded
# as 64-bit format, packing two 32-bit ROMENTRYs into a single read
# and dropping the high half of every entry on the floor.
_DEVID_FORMAT_MASK = 0xF
_DEVID_FORMAT_64BIT = 0x1

# Class 0x9 ROM Tables are identified by ARCHID = 0x0AF7 (architect
# = ARM JEP106 0x23B). Register against devarch_db so any class-0x9
# component matching this DEVARCH spawns a RomTable.
@MemoryMappedComponent.devarch_db.register(DevArch(architect=0x23B, archid=0x0AF7, revision=0, present=True))

# Class 0x1 ROM Tables don't have DEVARCH or DEVTYPE — they're
# detected purely by CIDR.CLASS. See model._pick_class for the
# Class 0x1 fallback to RomTable (lazy-imports this module).

class RomTable(MemoryMappedComponent):
    """CoreSight ROM Table — a list of pointers to other components.

    Class 0x1 ROM Tables (D2.2): 32-bit entries; the table ends on the
    first ROMENTRY whose value is 0x00000000 (PRESENT==0 with any
    other bit set is a not-present slot, must be skipped).

    Class 0x9 ROM Tables (D3.2): 32 or 64-bit entries selected by
    ``DEVID.FORMAT``; ARCHID = 0x0AF7 identifies them. PRESENT is a
    2-bit field: 0b00 = final entry (table end), 0b10 = not present
    (skip), 0b11 = present, 0b01 = reserved.

    Each present entry is classified via :data:`soc_db` (per-SoC
    override) or the standard :class:`MemoryMappedComponent` lookup
    precedence. Failures install a :class:`PowerGate`."""

    FRIENDLY_NAME = "ROM Table"

    # Per-SoC override registry keyed on (parent ROM PartId, child
    # absolute address). Lets vendors supply specific drivers for
    # generic components placed at well-known addresses.
    soc_db: Db = Db("ROM Table SoC override")

    # Maximum offset for ROM entries by class. Class 0x1: management
    # register space starts at 0xF00 so entries occupy 0x000..0xEFC.
    # Class 0x9: reserved area starts at 0x800 so entries occupy
    # 0x000..0x7FC (or 0x7F8 for 64-bit).
    __ENTRY_AREA_END_CLASS_1 = 0xF00
    __ENTRY_AREA_END_CLASS_9 = 0x800

    @property
    def entry_size(self) -> int:
        """Bytes per ROM entry: 4 for Class 0x1; for Class 0x9, 4 or
        8 depending on DEVID.FORMAT (bits[3:0], not bit[4])."""
        if self.cidr_class == self.CLASS_ROM_TABLE:
            return 4
        if self.devid is None:
            return 4
        format_field = self.devid & _DEVID_FORMAT_MASK
        if format_field == _DEVID_FORMAT_64BIT:
            return 8
        return 4

    async def start(self):
        size = self.entry_size
        self.logger.info(
            "ROM Table at 0x%x: cidr=%s, %d-bit entries, partid=%s",
            self.base, self.cidr_class, size * 8, self.partid.pretty())
        await self.__walk(size)

    # Width (in nibbles) used when formatting OFFSET/child addresses
    # in entry-trace lines. Sized for 64-bit entries; 32-bit entries
    # zero-extend in the same field, which keeps log columns aligned.
    __LOG_ADDR_NIBBLES = 16

    async def __walk(self, entry_size: int):
        is_class_9 = self.cidr_class == self.CLASS_CORESIGHT
        entry_area_end = (self.__ENTRY_AREA_END_CLASS_9 if is_class_9
                          else self.__ENTRY_AREA_END_CLASS_1)
        offset = 0
        while offset < entry_area_end:
            try:
                entry = await self.__read_entry(offset, entry_size)
            except DpAccessFailure as exc:
                self.logger.warning(
                    "ROM entry +0x%x read failed: %s — stopping walk",
                    offset, exc)
                return

            # Termination depends on the ROM Table class. Common
            # fields decoded once for the trace line either way.
            present_bits = entry & 0x3
            powerid_valid = bool((entry >> 2) & 1)
            powerid = (entry >> 4) & 0x1f
            child_offset = self.__sign_extend_offset(entry, entry_size)
            addr_size_bits = getattr(self._bus, "addr_size_bits", 64)
            addr_mask = (1 << addr_size_bits) - 1
            child_addr = (self.base + child_offset) & addr_mask

            # Class 0x1 (D2.2.2): ROMENTRY value 0 = end of table;
            # PRESENT (bit[0]) = 0 with any other bit set is a
            # not-present slot — skip and continue.
            #
            # Class 0x9 (D3.2.2 / D3.5.18): PRESENT[1:0] = 0b00 =
            # final entry (all other fields RES0); 0b10 = not present
            # (skip); 0b11 = present; 0b01 reserved.
            if is_class_9:
                if present_bits == 0b00:
                    self.logger.trace(
                        "ROM entry +0x%03x = 0x%0*x: end of ROM Table",
                        offset, entry_size * 2, entry)
                    return
                present = (present_bits == 0b11)
                state = {0b01: "rsvd", 0b10: "abs", 0b11: "ok"}[present_bits]
            else:
                if entry == 0:
                    self.logger.trace(
                        "ROM entry +0x%03x = 0x%0*x: end of ROM Table",
                        offset, entry_size * 2, entry)
                    return
                # Class 0x1 PRESENT is bit[0] alone; bit[1] is FORMAT
                # (RAO=1 in any non-terminator entry).
                present = bool(entry & 0x1)
                state = "ok" if present else "abs"

            # Single per-entry trace dump: raw value + every parsed
            # field (per IHI0074F D2.4.4 / D3.5.18). On a 32-bit
            # entry, the high address nibbles read as zero — we keep
            # the wider format so 32-bit and 64-bit entries align in
            # mixed logs.
            self.logger.trace(
                "ROM entry +0x%03x = 0x%0*x: %s "
                "POWERID=0x%02x/VALID=%d "
                "0x%0*x",
                offset, entry_size * 2, entry, state,
                powerid, powerid_valid,
                self.__LOG_ADDR_NIBBLES, child_addr)

            if not present:
                if is_class_9 and present_bits == 0b01:
                    self.logger.warning(
                        "ROM entry +0x%x: reserved PRESENT=0b01 — skipping",
                        offset)
                offset += entry_size
                continue

            # Spec D2.3.2 (Class 0x1) / D3.3.2 (Class 0x9): when
            # POWERIDVALID is set, the entry sits in a power domain
            # identified by POWERID and the debugger must request
            # power before the component is accessible. We don't yet
            # drive the power Requester — surface a one-liner at
            # info level so the operator can correlate WAIT/FAULT
            # responses with gated domains without -vvvv.
            if powerid_valid:
                self.logger.info(
                    "ROM entry +0x%x → 0x%x: POWERID=0x%x "
                    "(power-domain gated, requester not yet driven)",
                    offset, child_addr, powerid)

            await self.__discover_child(child_addr)
            offset += entry_size

    async def __read_entry(self, offset: int, entry_size: int) -> int:
        """Read a ROM entry as an int. 64-bit entries combine two
        32-bit reads (low word at +offset, high word at +offset+4)."""
        if entry_size == 4:
            return await self.reg_read(offset)
        lo = await self.reg_read(offset)
        hi = await self.reg_read(offset + 4)
        return (hi << 32) | lo

    @staticmethod
    def __sign_extend_offset(entry: int, entry_size: int) -> int:
        """Extract the signed byte offset from a ROM entry."""
        if entry_size == 4:
            raw = entry & 0xFFFFF000
            if raw & 0x80000000:
                raw -= 1 << 32
            return raw
        # 64-bit: OFFSET[63:12] is in entry[63:12].
        raw = entry & 0xFFFFFFFFFFFFF000
        if raw & (1 << 63):
            raw -= 1 << 64
        return raw

    async def __discover_child(self, addr: int):
        """Read the child's IDs and pick a class. On any failure,
        install a PowerGate."""
        try:
            ids = await ComponentIds.read(self._bus, addr)
        except DpAccessFailure as exc:
            self.logger.warning(
                "Child at 0x%x: ID read failed: %s — installing PowerGate",
                addr, exc)
            self.child_add(PowerGate(self._bus, addr, FailureKind.FAULT))
            return

        if ids.cidr_class is None:
            self.logger.info(
                "Child at 0x%x: no CIDR preamble — installing PowerGate",
                addr)
            self.child_add(PowerGate(self._bus, addr, FailureKind.EMPTY))
            return

        chosen = self.__classify(ids, addr)

        try:
            child = chosen(self._bus, addr, ids)
        except Exception as exc:
            self.logger.warning(
                "Child at 0x%x: %s.__init__ raised %s — installing PowerGate",
                addr, chosen.__name__, exc, exc_info=True)
            self.child_add(PowerGate(self._bus, addr, FailureKind.FAULT))
            return

        # Attach + await the child's start_tree before walking the
        # next entry. Some components (e.g. Cortex-M's SCS, which
        # sets DEMCR.TRCENA) gate the visibility of sibling
        # components listed *after* them in the ROM table —
        # without an explicit await here, child_add's
        # ensure_future-scheduled start_tree races against the
        # walker's next CIDR read and loses non-deterministically
        # depending on adapter batching (J-Link's one-USB-call-
        # per-batch tends to win the race; CMSIS-DAP's per-transfer
        # USB calls always lose). start_tree is idempotent
        # (_started guard), so the parallel ensure_future invocation
        # from child_add is harmless.
        self.child_add(child)
        try:
            await child.start_tree()
        except Exception as exc:
            self.logger.warning(
                "Child at 0x%x: start_tree raised %s — sibling "
                "discovery may be incomplete",
                addr, exc, exc_info=True)

    def __classify(self, ids: ComponentIds, addr: int):
        """Pick a class for the child at ``addr`` given its IDs.
        Per-SoC override (this ROM's PartId + child address) wins
        over the standard precedence."""
        soc_key = (self.partid, addr)
        try:
            handlers = self.soc_db.get(soc_key, allow_default=False)
            return handlers[0]
        except NoMatch:
            pass
        return _pick_class(ids)


