"""Cortex-A processor debug logic ("Debug Management").

DEVTYPE = 0x15 (Debug Logic, processor) covers Cortex-A8/A9 debug
on class-0x9 components without DEVARCH. DEVARCH ARCHID = 0x6A15
identifies the ARMv8 processor debug architecture (Cortex-A53/A57/...
EDLAR/EDSCR register set)."""

from .model import CoresightComponent, DevArch, MemoryMappedComponent

@MemoryMappedComponent.devarch_db.register(DevArch(architect=0x23B, archid=0x6A15, revision=0, present=True))
@CoresightComponent.db.register(0x15)
class Dbg(CoresightComponent):
    FRIENDLY_NAME = "Debug Management"
