"""EZ-USB FX3 boot image (.img), the firmware format the FX3 BootROM
loads from I2C EEPROM, SPI flash or USB.

Layout, all words little-endian:

    +0  "CY"
    +2  bImageCTL   boot device size / speed selection, bit 0 clear
                    for an executable image
    +3  bImageType  0xb0, firmware with checksum
    then sections:  dLength (words), dAddress, dLength data words
    last section:   dLength = 0, dAddress = entry point
    then:           32-bit sum of all data words

The parser exposes the sections as `region/N` Addressable children
and the entry point as `entry` metadata, so an image loads to RAM
like an ELF does.
"""

import struct

from ...db import NoMatch
from ...memory_map import MemoryMap
from ...node import Addressable, Node, Readable
from ...vfs import FormatNode, register_format, register_magic


class Fx3BootImageRegion(Node, Readable, Addressable):
    def __init__(self, name, address, data: bytes):
        super().__init__(name)
        self.__address = address
        self.__data = data

    @property
    def size(self) -> int:
        return len(self.__data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self.__data):
            raise ValueError(f"offset {offset} out of range")
        return self.__data[offset:offset + size]

    @property
    def load_address(self) -> int:
        return self.__address


@register_format("fx3_img")
class Fx3BootImage(FormatNode):
    SIGNATURE = b"CY"
    TYPE_FIRMWARE = 0xb0
    CTL_DATA = 1 << 0

    SECTION_MAX = 4096

    @classmethod
    def magic(cls, head: bytes) -> bool:
        return (len(head) >= 4 and head[:2] == cls.SIGNATURE
                and not head[2] & cls.CTL_DATA
                and head[3] == cls.TYPE_FIRMWARE)

    @classmethod
    def encode(cls, memory_map: MemoryMap, image_ctl: int) -> bytes:
        """Build an image from `memory_map`, which must carry an entry
        point. Sections are split to at most SECTION_MAX bytes and
        padded to whole words."""
        if memory_map.entry is None:
            raise ValueError("FX3 boot image needs an entry point")
        if image_ctl & cls.CTL_DATA or not 0 <= image_ctl <= 0xff:
            raise ValueError(f"bImageCTL {image_ctl:#x} is not an executable image")
        out = bytearray(cls.SIGNATURE + bytes([image_ctl, cls.TYPE_FIRMWARE]))
        checksum = 0
        for address, data in memory_map.simplified().chunks:
            if address % 4:
                raise ValueError(f"section at {address:#x} is not word aligned")
            for offset in range(0, len(data), cls.SECTION_MAX):
                blob = data[offset:offset + cls.SECTION_MAX]
                blob += bytes(-len(blob) % 4)
                words = struct.unpack(f"<{len(blob) // 4}L", blob)
                out += struct.pack("<2L", len(words), address + offset) + blob
                checksum += sum(words)
        out += struct.pack("<3L", 0, memory_map.entry, checksum & 0xffffffff)
        return bytes(out)

    async def start(self):
        raw = await self._source.read(0, self._source.size)
        if not self.magic(raw):
            raise NoMatch("fx3_img", "signature")
        self.metadata["image_ctl"] = raw[2]
        regions = Node("region")
        offset = 4
        checksum = 0
        while True:
            if offset + 8 > len(raw):
                raise ValueError(f"{self.fqdn}: truncated at {offset:#x}")
            length, address = struct.unpack_from("<2L", raw, offset)
            offset += 8
            if not length:
                break
            data = raw[offset:offset + 4 * length]
            if len(data) != 4 * length:
                raise ValueError(f"{self.fqdn}: truncated section at {address:#x}")
            checksum += sum(struct.unpack(f"<{length}L", data))
            regions.child_add(Fx3BootImageRegion(
                str(len(regions.children)), address, data))
            offset += 4 * length
        if offset + 4 > len(raw):
            raise ValueError(f"{self.fqdn}: missing checksum")
        expected, = struct.unpack_from("<L", raw, offset)
        if expected != checksum & 0xffffffff:
            raise ValueError(f"{self.fqdn}: bad checksum")
        self.metadata["entry"] = address
        self.child_add(regions)


@register_magic
def _fx3_img_magic(head: bytes):
    return "fx3_img" if Fx3BootImage.magic(head) else None
