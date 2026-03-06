from ...protocol.jtag import Tap
from .series7 import Series7

_PARTS = {
    0x03622093: "S6",
    0x03620093: "S15",
    0x037c4093: "S25",
    0x0362f093: "S50",
    0x037c8093: "S75",
    0x037c7093: "S100",
}


@Tap.db.register(*_PARTS.keys())
class Spartan7(Series7):
    def __init__(self, interface, idcode, **kw):
        name = "Spartan7-" + _PARTS.get(idcode, f"0x{idcode:08x}")
        super().__init__(interface, idcode, name=name, **kw)
