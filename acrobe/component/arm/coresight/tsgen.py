"""CoreSight Time Stamp Generator (TSGen / generic counter module).

Free-running 64-bit counter ARM defines for global timestamp
distribution to ITM, ETM and other trace sources. Identified by
ARM PartId 0x101 — appears on the APB-debug side of v8-M SoCs
(M55/M85) where the trace-side ROM exposes a TSGen alongside the
trace funnel + ETB."""

from .model import MemoryMappedComponent, PartId


class TimeStampGenerator(MemoryMappedComponent):
    FRIENDLY_NAME = "Time Stamp Generator"


MemoryMappedComponent.db.register(
    PartId(jep106_bank=4, jep106_id=0x3B, part_no=0x101)
)(TimeStampGenerator)
