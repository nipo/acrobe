import asyncio

from ...protocol.jtag import Tap, Dr, Instruction
from ...part_id import PartId
from ..fpga import JtagSramFpga
from ...bitstring import BitString
from ...util.endian import bitswap8
from ...bitfield import Bitfield, BooleanField, MappingField, Field

class PartInfo:
    def __init__(self, idcode, name, col_bit_count, row_count, flash_page_count, ufm_page_count):
        self.idcode = idcode
        self.name = name
        self.col_bit_count = col_bit_count
        self.row_count = row_count
        self.flash_page_count = flash_page_count
        self.ufm_page_count = ufm_page_count

# Relevant info in data/vmdata/database/xpga/xo2/{ispVM_018a.xdf,XO2.svp}
PARTS = [
    PartInfo(0x012B0043, "LCMXO2-256ZE", 504, 186, 575, 0),
    PartInfo(0x012B8043, "LCMXO2-256HC", 504, 186, 575, 0),
    PartInfo(0x012B1043, "LCMXO2-640ZE", 888, 215, 1151, 192),
    PartInfo(0x012B9043, "LCMXO2-640HC", 888, 215, 1151, 192),
    PartInfo(0x012BA043, "LCMXO2-640UHC", 1080, 333, 2175, 512),
    PartInfo(0x012B2043, "LCMXO2-1200ZE", 1080, 333, 2175, 512),
    PartInfo(0x012BA043, "LCMXO2-1200HC", 1080, 333, 2175, 512),
    PartInfo(0x012B3043, "LCMXO2-2000ZE", 1272, 420, 3198, 640),
    PartInfo(0x012BB043, "LCMXO2-1200UHC", 1272, 420, 3198, 640),
    PartInfo(0x012BB043, "LCMXO2-2000HC", 1272, 420, 3198, 640),
    PartInfo(0x012B3043, "LCMXO2-2000HE", 1272, 420, 3198, 640),
    PartInfo(0x012B4043, "LCMXO2-4000ZE", 1560, 623, 5758, 768),
    PartInfo(0x012BC043, "LCMXO2-2000UHC", 1560, 623, 5758, 768),
    PartInfo(0x012BC043, "LCMXO2-4000HC", 1560, 623, 5758, 768),
    PartInfo(0x012B4043, "LCMXO2-2000UHE", 1560, 623, 5758, 768),
    PartInfo(0x012B4043, "LCMXO2-4000HE", 1560, 623, 5758, 768),
    PartInfo(0x012B5043, "LCMXO2-7000HE", 1992, 770, 9212, 2048),
    PartInfo(0x012B5043, "LCMXO2-7000ZE", 1992, 770, 9212, 2048),
    PartInfo(0x012BD043, "LCMXO2-4000UHC", 1992, 770, 9212, 2048),
    PartInfo(0x012BD043, "LCMXO2-7000HC", 1992, 770, 9212, 2048),
]        

class MachXO2Status(Bitfield):
    TransparentMode = BooleanField(0)
    ConfigTarget    = Field(1, 3)
    JTAG            = BooleanField(4)
    Done            = BooleanField(8)
    ISC             = BooleanField(9)
    Write           = BooleanField(10)
    Read            = BooleanField(11)
    Busy            = BooleanField(12)
    Fail            = BooleanField(13)
    StdPreamble     = BooleanField(21)
    SPIFail         = BooleanField(22)
    BSEError        = MappingField(23, 3,
                        ["OK", "ID", "CMD", "CRC", "Preamble",
                         "Abort", "Overflow", "SDM"])
    ExecutionError  = BooleanField(26)


@Tap.db.register(*[PartId.from_idcode(p.idcode) for p in PARTS])
class MachXO2(Tap, JtagSramFpga):
    irlen = 8

    def __init__(self, idcode, **kw):
        possible = [p for p in PARTS if int(p.idcode) == idcode]
        if possible:
            name = possible[0].name
        else:
            name = f"MachXO2-0x{idcode:08x}"
        super().__init__(idcode=idcode, name=name, **kw)
