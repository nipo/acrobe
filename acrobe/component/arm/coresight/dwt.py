"""Data Watchpoint and Trace (DWT). Cortex-M data-watchpoint and
PC-sample/exception-trace block.

ARM PartId 0x002 (Cortex-M3/M4 DWT). ARMv8-M advertises via
DEVARCH ARCHID = 0x1A02."""

from .model import DevArch, MemoryMappedComponent, PartId


class Dwt(MemoryMappedComponent):
    FRIENDLY_NAME = "Data Watchpoint and Trace"


MemoryMappedComponent.db.register(
    PartId(jep106_continuation=4, jep106_id=0x3B, part_no=0x002)
)(Dwt)


MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x1A02, revision=0, present=True)
)(Dwt)
