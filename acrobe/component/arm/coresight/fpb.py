"""Flash Patch and Breakpoint (FPB). Cortex-M instruction-address
comparator + literal-patch block. Provides hardware breakpoints.

ARM PartId 0x003 (Cortex-M3/M4 FPB). The simpler BPU on Cortex-M0+
uses PartId 0x00B and exposes a subset of the FPB programmer's
model — same friendly label. ARMv8-M (M55/M85) advertises via
DEVARCH ARCHID = 0x1A03 (BPU; same role, no flash-patch)."""

from .model import DevArch, MemoryMappedComponent, PartId


class Fpb(MemoryMappedComponent):
    FRIENDLY_NAME = "Flash Patch and Breakpoint"


for _part in (0x003,   # Cortex-M3 / M4 FPB
              0x00B):  # Cortex-M0+ BPU
    MemoryMappedComponent.db.register(
        PartId(jep106_bank=4, jep106_id=0x3B, part_no=_part)
    )(Fpb)


# ARMv8-M BPU — same role, identified by DEVARCH instead of PartId.
MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x1A03, revision=0, present=True)
)(Fpb)
