"""EZ-USB FX3 debug TAP: a plain ARM926EJ-S TAP under a Cypress
IDCODE."""

from ...part_id import PartId
from ...protocol.jtag import Tap
from ..arm.arm9.tap import Arm9Tap


@Tap.db.register(PartId(0, 0x34, 0x7926))
class Fx3Tap(Arm9Tap):
    max_freq = 25e6

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "FX3"
        super().__init__(idcode=idcode, irlen=irlen, name=name)
