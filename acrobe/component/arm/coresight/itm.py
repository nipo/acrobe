"""Instruction Trace Macrocell (ITM). Software-generated trace
source on Cortex-M cores.

DEVTYPE = 0x63 (Trace source, software stimulus) covers ITM on
class-0x9 components. The Cortex-M3/M4 ITM is also identifiable by
its ARM PartId — 0x001 — for components that don't have DEVTYPE."""

from .model import CoresightComponent, MemoryMappedComponent, PartId

# Cortex-M3 / M4 ITM as a class-0xE / class-0xF component identified
# by its ARM PartId (no DEVTYPE).
@MemoryMappedComponent.db.register(PartId.from_idcode(0x001477))
@CoresightComponent.db.register(0x63)
class Itm(CoresightComponent):
    FRIENDLY_NAME = "Instruction Trace Macrocell"
