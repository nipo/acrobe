"""Cross-Trigger Interface (CTI). DEVTYPE = 0x14 (Debug control,
Trigger Matrix). Routes debug events between CoreSight components
(halt-on-trace-trigger, run-on-cross-debug-stop, etc.).

DEVARCH ARCHID = 0x1A14 identifies CTIv2 on ARMv8."""

from .model import CoresightComponent, DevArch, MemoryMappedComponent


@CoresightComponent.db.register(0x14)
class Cti(CoresightComponent):
    FRIENDLY_NAME = "Cross-Trigger Interface"


MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x1A14, revision=0, present=True)
)(Cti)
