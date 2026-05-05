"""CoreSight Time Stamp Generator (TSGen / generic counter module).

Free-running 64-bit counter ARM defines for global timestamp
distribution to ITM, ETM and other trace sources. Two ARM IP
variants register the same role:

* PartId 0x101 — the older standalone CoreSight TSGen, found on
  the APB-debug side of v8-M SoCs (M55/M85) where the trace-side
  ROM exposes a TSGen alongside the trace funnel + ETB.
* PartId 0x193 — ARM SoC-600 ``css600_tsgen``. Same programmers'
  model role; observed on the Agilex 5 HPS APB4 fabric."""

from .model import MemoryMappedComponent, PartId

@MemoryMappedComponent.db.register(PartId.from_idcode(0x101477))
@MemoryMappedComponent.db.register(PartId.from_idcode(0x193477))
class TimeStampGenerator(MemoryMappedComponent):
    FRIENDLY_NAME = "Time Stamp Generator"

