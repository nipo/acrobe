"""System Control Space (SCS). Cortex-M debug + control register
bank at the well-known address 0xE000_E000.

Identified by ARM PartId — different cores have different part_no:
0x000 (Cortex-M3), 0x008 (Cortex-M0), 0x009 (Cortex-M0+),
0x00C (Cortex-M4), 0x00D (Cortex-M7), 0x00E (Cortex-M33),
etc. ARMv8-M cores additionally advertise via DEVARCH
ARCHID = 0x2A04."""

from .model import DevArch, MemoryMappedComponent, PartId


class Scs(MemoryMappedComponent):
    FRIENDLY_NAME = "System Control Space"


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
