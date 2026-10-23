"""ARM9E-S / ARM9EJ-S debug TAP."""

from ....part_id import PartId
from ....protocol.jtag import Dr, Instruction, Tap
from .debug import Arm9Debug
from .scan_chain import Cp15Chain, DebugChain, IceChain


@Tap.db.register(PartId.from_idcode(0x07926f0f))
class Arm9Tap(Tap):
    """Debug TAP of an ARM9E-S family core, with an
    :class:`~.debug.Arm9Debug` child driving its scan chains."""

    irlen = 4

    SCAN_N_DR = Dr(5)
    DEBUG_DR = Dr(DebugChain.LENGTH)
    ICE_DR = Dr(IceChain.LENGTH)
    CP15_DR = Dr(Cp15Chain.LENGTH)
    IDCODE_DR = Dr(32)

    SCAN_N = Instruction(0x2, "SCAN_N_DR")
    RESTART = Instruction(0x4, "BYPASS_REG")
    INTEST_DEBUG = Instruction(0xc, "DEBUG_DR")
    INTEST_ICE = Instruction(0xc, "ICE_DR")
    INTEST_CP15 = Instruction(0xc, "CP15_DR")
    IDCODE = Instruction(0xe, "IDCODE_DR")

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "ARM9"
        super().__init__(idcode=idcode, irlen=irlen, name=name)

    async def start(self):
        self.child_add(Arm9Debug())

    async def pre_tlr(self):
        for debug in self.children_of_class(Arm9Debug):
            debug.chain_forget()
