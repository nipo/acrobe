"""Flash Patch and Breakpoint (FPB). Cortex-M instruction-address
comparator + literal-patch block. Provides hardware breakpoints.

ARM PartId 0x003 (Cortex-M3/M4 FPB). The simpler BPU on Cortex-M0+
uses PartId 0x00B and exposes a subset of the FPB programmer's
model — same friendly label. ARMv8-M (M55/M85) advertises via
DEVARCH ARCHID = 0x1A03 (BPU; same role, no flash-patch)."""

from .model import DevArch, MemoryMappedComponent, PartId

# ARMv8-M BPU — same role, identified by DEVARCH instead of PartId.
@MemoryMappedComponent.devarch_db.register(DevArch(architect=0x23B, archid=0x1A03, revision=0, present=True))
@MemoryMappedComponent.db.register(PartId.from_idcode(0x003477))
@MemoryMappedComponent.db.register(PartId.from_idcode(0x00b477))
class Fpb(MemoryMappedComponent):
    FRIENDLY_NAME = "Flash Patch and Breakpoint"
