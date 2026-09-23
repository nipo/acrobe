"""NSL virtual JTAG nodes behind an Altera SLD hub.

NSL nodes report JEP106 bank 11, code 0x7f, a code JEP106 never assigns
in any bank, so they cannot collide with a vendor's.  The type then
names what the node carries, as ``nsl_jtag.user_tap`` assigns it.
"""

from acrobe.part_id import PartId

def nsl_part_id(type_id: int) -> PartId:
    return PartId(jep106_bank = 0xb, jep106_id = 0x7f, part_no = type_id)
