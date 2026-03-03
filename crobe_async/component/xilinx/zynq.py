from ...protocol.jtag import Tap
from .series7 import Series7

_PARTS = {
    0x03723093: "007",
    0x03722093: "010",
    0x0373c093: "012",
    0x03728093: "014",
    0x0373b093: "015",
    0x03727093: "020",
    0x0372c093: "030",
    0x03732093: "035",
    0x03731093: "045",
    0x03736093: "100",
}


@Tap.db.register(*_PARTS.keys())
class Zynq(Series7):
    def __init__(self, interface, idcode, **kw):
        name = "Zynq-" + _PARTS.get(idcode, f"0x{idcode:08x}")
        super().__init__(interface, idcode, name=name, **kw)
