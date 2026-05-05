"""Embedded Trace Macrocell (ETM). Per-CPU instruction trace source.

DEVTYPE = 0x13 (Trace source, associated with a processor core)
covers ETMv3-era components without DEVARCH (Cortex-A9 and similar).
DEVARCH ARCHID = 0x4A13 identifies ETMv4+ on ARMv8 cores. Both
classifications get the same friendly label here."""

from .model import CoresightComponent, DevArch, MemoryMappedComponent

# ETMv4 advertises itself via DEVARCH; same friendly name.
@MemoryMappedComponent.devarch_db.register(DevArch(architect=0x23B, archid=0x4A13, revision=0, present=True))
@CoresightComponent.db.register(0x13)
class Etm(CoresightComponent):
    FRIENDLY_NAME = "Embedded Trace Macrocell"
