"""System Control Space (SCS). Cortex-M debug + control register
bank at the well-known address 0xE000_E000.

Identified by ARM PartId — different cores have different part_no:
0x000 (Cortex-M3), 0x008 (Cortex-M0), 0x009 (Cortex-M0+),
0x00C (Cortex-M4), 0x00D (Cortex-M7), 0x00E (Cortex-M33),
etc. ARMv8-M cores additionally advertise via DEVARCH
ARCHID = 0x2A04."""

from ..dp import DpAccessFailure
from .model import DevArch, MemoryMappedComponent, PartId


class Scs(MemoryMappedComponent):
    FRIENDLY_NAME = "System Control Space"

    # Debug Exception and Monitor Control Register (within SCS, at
    # offset 0xDFC from SCS_BASE = 0xE000_E000).
    DEMCR_OFFSET = 0xDFC
    DEMCR_TRCENA = 1 << 24

    async def start(self) -> None:
        """Set DEMCR.TRCENA so ITM/DWT/TPIU come out of reset and
        their CoreSight ID registers become readable.

        Started automatically when this Scs is parented to its
        ROM Table (Node tree's child_add → start_tree on a started
        parent). Sibling ROM-table entries that depend on TRCENA
        (the trace components, typically listed after the SCS) get
        enumerated correctly as long as the SCS appears in the
        ROM Table before its dependent modules."""
        addr = self.base + self.DEMCR_OFFSET
        try:
            demcr = await self._bus.read32(addr)
        except DpAccessFailure as exc:
            self.logger.warning(
                "DEMCR read failed (0x%x): %s — trace components "
                "may not enumerate", addr, exc)
            return
        if demcr & self.DEMCR_TRCENA:
            return
        self.logger.info(
            "Enabling DEMCR.TRCENA (DEMCR was 0x%08x) so trace "
            "components are accessible", demcr)
        try:
            await self._bus.write32(addr, demcr | self.DEMCR_TRCENA)
        except DpAccessFailure as exc:
            self.logger.warning(
                "DEMCR write failed: %s — trace components may "
                "not enumerate", exc)


for _part in (0x000,  # Cortex-M3
              0x008,  # Cortex-M0
              0x009,  # Cortex-M0+
              0x00C,  # Cortex-M4
              0x00D,  # Cortex-M7
              0x00E,  # Cortex-M33
              0x471,  # Cortex-M1 (legacy)
              0x4C8): # Cortex-M55
    MemoryMappedComponent.db.register(
        PartId(jep106_bank=4, jep106_id=0x3B, part_no=_part)
    )(Scs)


# ARMv8-M debug architecture.
MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x2A04, revision=0, present=True)
)(Scs)
