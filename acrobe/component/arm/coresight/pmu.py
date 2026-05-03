"""Performance Monitoring Unit (PMU). Per-core counter block.

DEVTYPE = 0x16 (Performance Monitor, processor) covers PMUv1/v2 on
class-0x9 components. DEVARCH ARCHID = 0x2A16 identifies PMUv3+ on
ARMv8 cores."""

from .model import CoresightComponent, DevArch, MemoryMappedComponent


@CoresightComponent.db.register(0x16)
class Pmu(CoresightComponent):
    FRIENDLY_NAME = "Performance Monitoring Unit"


MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x2A16, revision=0, present=True)
)(Pmu)
