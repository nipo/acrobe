"""Bus trace source. DEVTYPE = 0x43 (Trace source, associated with
a bus, stimulus derived from bus activity)."""

from .model import CoresightComponent


@CoresightComponent.db.register(0x43)
class BusTrace(CoresightComponent):
    FRIENDLY_NAME = "Coresight Bus Trace Source"
