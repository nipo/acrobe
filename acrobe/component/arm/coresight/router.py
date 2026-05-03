"""Basic trace router. DEVTYPE = 0x31 (Trace sink, basic trace
router) — a minimal CoreSight component that routes ATB without
buffering. DEVARCH ARCHID = 0x0A31 identifies it on ADIv6 chips."""

from .model import CoresightComponent, DevArch, MemoryMappedComponent


@CoresightComponent.db.register(0x31)
class TraceRouter(CoresightComponent):
    FRIENDLY_NAME = "Basic Trace Router"


MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x0A31, revision=0, present=True)
)(TraceRouter)
