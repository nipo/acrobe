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


# DEVID.FORMAT (Class 0x9 ROM Tables): bit selecting 32-bit vs 64-bit
# entries. The spec places this at DEVID[4] for Class 0x9 ROM Tables.
_DEVID_FORMAT_BIT = 1 << 4


class RomTable(MemoryMappedComponent):
    """CoreSight ROM Table — a list of pointers to other components.

    Class 0x1 ROM Tables: 32-bit entries (legacy ADIv5 layout).
    Class 0x9 ROM Tables: 32 or 64-bit entries; format is in
    ``DEVID.FORMAT``. ARCHID = 0x0AF7 identifies them.

    The walker iterates entries until it hits a present=0b00
    terminator (or the management-register area at +0xF00). Each
    present entry is classified via :data:`soc_db` (per-SoC override)
    or the standard :class:`MemoryMappedComponent` lookup precedence.
    Failures install a :class:`PowerGate`."""

    FRIENDLY_NAME = "ROM Table"

    # Per-SoC override registry keyed on (parent ROM PartId, child
    # absolute address). Lets vendors supply specific drivers for
    # generic components placed at well-known addresses.
    soc_db: Db = Db("ROM Table SoC override")

    # Maximum offset for ROM entries — management registers begin
    # at +0xF00 on Class 0x1 ROM Tables. We use the same upper
    # bound for both classes; entries are terminated by the first
    # PRESENT=0b00 anyway.
    _ENTRY_AREA_END = 0xF00

    @property
    def entry_size(self) -> int:
        """Bytes per ROM entry: 4 for Class 0x1 and 32-bit Class 0x9,
        8 for 64-bit Class 0x9."""
        if self.cidr_class == self.CLASS_ROM_TABLE:
            return 4
        if self.devid is not None and (self.devid & _DEVID_FORMAT_BIT):
            return 8
        return 4

    async def start(self):
        size = self.entry_size
        self.logger.info(
            "ROM Table at 0x%x: cidr=%s, %d-bit entries, partid=%s",
            self.base, self.cidr_class, size * 8, self.partid.pretty())
        await self._walk(size)

    async def _walk(self, entry_size: int):
        offset = 0
        while offset < self._ENTRY_AREA_END:
            try:
                entry = await self._read_entry(offset, entry_size)
            except DpAccessFailure as exc:
                self.logger.warning(
                    "ROM entry +0x%x read failed: %s — stopping walk",
                    offset, exc)
                return

            present_bits = entry & 0x3
            if present_bits == 0b00:
                # End of table.
                return
            if present_bits != 0b11:
                self.logger.warning(
                    "ROM entry +0x%x has reserved present bits 0b%02b — skipping",
                    offset, present_bits)
                offset += entry_size
                continue

            child_offset = self._sign_extend_offset(entry, entry_size)
            child_addr = (self.base + child_offset) & 0xFFFFFFFFFFFFFFFF
            await self._discover_child(child_addr)
            offset += entry_size

    async def _read_entry(self, offset: int, entry_size: int) -> int:
        """Read a ROM entry as an int. 64-bit entries combine two
        32-bit reads (low word at +offset, high word at +offset+4)."""
        if entry_size == 4:
            return await self._bus.read32(self.base + offset)
        lo = await self._bus.read32(self.base + offset)
        hi = await self._bus.read32(self.base + offset + 4)
        return (hi << 32) | lo

    @staticmethod
    def _sign_extend_offset(entry: int, entry_size: int) -> int:
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

    async def _discover_child(self, addr: int):
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

        chosen = self._classify(ids, addr)

        try:
            child = chosen(self._bus, addr, ids)
        except Exception as exc:
            self.logger.warning(
                "Child at 0x%x: %s.__init__ raised %s — installing PowerGate",
                addr, chosen.__name__, exc, exc_info=True)
            self.child_add(PowerGate(self._bus, addr, FailureKind.FAULT))
            return

        self.child_add(child)

    def _classify(self, ids: ComponentIds, addr: int):
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


# Class 0x9 ROM Tables are identified by ARCHID = 0x0AF7 (architect
# = ARM JEP106 0x23B). Register against devarch_db so any class-0x9
# component matching this DEVARCH spawns a RomTable.
MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x0AF7, revision=0, present=True)
)(RomTable)


# Class 0x1 ROM Tables don't have DEVARCH or DEVTYPE — they're
# detected purely by CIDR.CLASS. See model._pick_class for the
# Class 0x1 fallback to RomTable (lazy-imports this module).
