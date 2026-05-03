"""Flash Patch and Breakpoint (FPB). Cortex-M instruction-address
comparator + literal-patch block. Provides hardware breakpoints.

ARM PartId 0x003 (Cortex-M3/M4 FPB). The simpler BPU on Cortex-M0+
uses PartId 0x00B and exposes a subset of the FPB programmer's
model — same friendly label."""

from .model import MemoryMappedComponent, PartId


class Fpb(MemoryMappedComponent):
    FRIENDLY_NAME = "Flash Patch and Breakpoint"


for _part in (0x003,   # Cortex-M3 / M4 FPB
              0x00B):  # Cortex-M0+ BPU
    MemoryMappedComponent.db.register(
        PartId(jep106_continuation=4, jep106_id=0x3B, part_no=_part)
    )(Fpb)
