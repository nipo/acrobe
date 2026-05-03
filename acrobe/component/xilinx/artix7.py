from ...protocol.jtag import Tap
from ...part_id import PartId
from .series7 import Series7

_PARTS = {
    0x0362e093: "XC7A15T",
    0x0362d093: "XC7A35T",
    0x0362c093: "XC7A50T",
    0x03631093: "XC7A100T",
}


@Tap.db.register(*map(PartId.from_idcode, _PARTS.keys()))
class Artix7(Series7):
    def __init__(self, idcode, **kw):
        name = _PARTS.get(idcode, f"Artix7-0x{idcode:08x}")
        super().__init__(idcode=idcode, name=name, **kw)
