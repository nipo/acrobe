from ...protocol.jtag import Tap
from .series6 import Series6

_PARTS = {
    0x04000093: "LX4",
    0x04001093: "LX9",
    0x04002093: "LX16",
    0x04004093: "LX25",
    0x04024093: "LX25T",
    0x04008093: "LX45",
    0x04028093: "LX45T",
    0x0400E093: "LX75",
    0x0402E093: "LX75T",
    0x04011093: "LX100",
    0x04031093: "LX100T",
    0x0401D093: "LX150",
    0x0403D093: "LX150T",
}


@Tap.db.register(*_PARTS.keys())
class Spartan6(Series6):
    def __init__(self, idcode, **kw):
        name = "Spartan6-" + _PARTS.get(idcode, f"0x{idcode:08x}")
        super().__init__(idcode=idcode, name=name, **kw)
