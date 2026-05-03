"""Coprocessor / data-engine trace source. DEVTYPE = 0x33 (Trace
source, associated with a Data Engine or Coprocessor)."""

from .model import CoresightComponent


@CoresightComponent.db.register(0x33)
class CoprocTrace(CoresightComponent):
    FRIENDLY_NAME = "Coresight Coproc Trace Source"
