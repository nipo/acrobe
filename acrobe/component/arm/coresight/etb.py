"""Embedded Trace Buffer (ETB). DEVTYPE = 0x21 (Trace sink, Buffer).
On-chip RAM that captures ATB trace data for later download."""

from .model import CoresightComponent


@CoresightComponent.db.register(0x21)
class Etb(CoresightComponent):
    FRIENDLY_NAME = "Embedded Trace Buffer"
