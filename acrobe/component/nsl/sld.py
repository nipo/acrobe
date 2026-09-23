"""NSL virtual JTAG nodes behind an Altera SLD hub.

NSL nodes report JEP106 bank 11, code 0x7f, a code JEP106 never assigns
in any bank, so they cannot collide with a vendor's.  The type then
names what the node carries, as ``nsl_jtag.user_tap`` assigns it.
"""

from acrobe.part_id import PartId

from ..altera.sld_hub import SldHub, SldNodeInfo
from .jtag_continuous_transport import ContinuousTransport


def nsl_part_id(type_id: int) -> PartId:
    return PartId(jep106_bank = 0xb, jep106_id = 0x7f, part_no = type_id)


# nsl_jtag.continuous_transport, carrying framed bytes nothing here
# knows the meaning of.
@SldHub.db.register(nsl_part_id(0x01))
def _continuous_transport(hub: SldHub, info: SldNodeInfo):
    return ContinuousTransport(
        hub.tap, hub.instruction(info, 0),
        name = f"continuous_transport{info.instance}")
