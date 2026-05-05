"""Data Watchpoint and Trace (DWT). Cortex-M data-watchpoint and
PC-sample/exception-trace block.

ARM PartId 0x002 (Cortex-M3/M4 DWT). ARMv8-M advertises via
DEVARCH ARCHID = 0x1A02."""

from .model import DevArch, MemoryMappedComponent, PartId

@MemoryMappedComponent.db.register(PartId.from_idcode(0x002477))
@MemoryMappedComponent.devarch_db.register(DevArch(architect=0x23B, archid=0x1A02, revision=0, present=True))
class Dwt(MemoryMappedComponent):
    FRIENDLY_NAME = "Data Watchpoint and Trace"
