"""Trace Funnel / Router. DEVTYPE = 0x12 (Trace link, Trace funnel).
Multiplexes multiple ATB trace streams into a single output."""

from .model import CoresightComponent


@CoresightComponent.db.register(0x12)
class Funnel(CoresightComponent):
    FRIENDLY_NAME = "Trace Funnel"
