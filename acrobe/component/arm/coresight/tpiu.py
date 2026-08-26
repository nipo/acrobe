"""Trace Port Interface Unit (TPIU). DEVTYPE = 0x11 (Trace sink,
Trace port). Routes formatted ATB trace off-chip via a parallel or
serial trace port."""

from .model import CoresightComponent


@CoresightComponent.db.register(0x11)
class Tpiu(CoresightComponent):
    FRIENDLY_NAME = "Trace Port Interface Unit"
