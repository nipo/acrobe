from ...protocol.jtag import Tap
from ...part_id import PartId
from .series7 import Series7

_PARTS = {
    0x0364c093: "160T",
}


@Tap.db.register(*map(PartId.from_idcode, _PARTS.keys()))
class Kintex7(Series7):
    def __init__(self, idcode, **kw):
        name = "Kintex7-" + _PARTS.get(idcode, f"0x{idcode:08x}")
        super().__init__(idcode=idcode, name=name, **kw)
