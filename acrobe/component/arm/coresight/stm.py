"""System Trace Macrocell (STM). Multi-master software trace source.

DEVARCH ARCHID = 0x0A63 identifies the STM architecture. Note that
DEVTYPE 0x63 covers both ITM and STM (both software-stimulus trace
sources); the DEVARCH-keyed registration here wins for components
that advertise the STM-specific architecture."""

from .model import DevArch, MemoryMappedComponent


class Stm(MemoryMappedComponent):
    FRIENDLY_NAME = "System Trace Macrocell"


MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x0A63, revision=0, present=True)
)(Stm)
