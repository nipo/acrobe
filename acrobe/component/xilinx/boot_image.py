"""Zynq-7000 boot image (BOOT.BIN) as a VFS Node.

Layout per UG585 "Boot Header" and UG821 "Zynq-7000 Boot Image
Format": a boot header the BootROM reads to find the FSBL, a register
initialization table, then the image header and partition header
tables the FSBL walks.

Children:
- fsbl: the image the BootROM loads, from the boot header.
- partition/N: one per partition header, in image order. Partition 0
  is the FSBL itself. PS partitions are plain Readables; PL
  partitions hold a `bitstream` child in .bit byte order.

None of them are Addressable: a boot image is not a memory image, the
partitions are placed by whoever plays the BootROM and FSBL roles.

Metadata: `register_init`, the (address, value) pairs the BootROM
writes before loading the FSBL.
"""

import enum
import struct

from ...db import NoMatch
from ...node import Node, Readable
from ...vfs import FormatNode, register_format, register_magic


class Destination(enum.IntEnum):
    PS = 1
    PL = 2


class BootImageSlice(Node, Readable):
    def __init__(self, name, source, offset, size):
        super().__init__(name)
        self.__source = source
        self.__offset = offset
        self.__size = size

    @property
    def size(self) -> int:
        return self.__size

    async def read(self, offset, size):
        if offset < 0 or offset > self.__size:
            raise ValueError(f"offset {offset} out of range")
        n = min(size, self.__size - offset)
        if n <= 0:
            return b""
        return await self.__source.read(self.__offset + offset, n)


class BootImageBitstream(Node, Readable):
    """PL partition data with each 32-bit word byte-swapped back to
    configuration stream order."""

    def __init__(self, name, data: bytes):
        super().__init__(name)
        self.__data = b"".join(data[i:i + 4][::-1]
                               for i in range(0, len(data), 4))

    @property
    def size(self) -> int:
        return len(self.__data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self.__data):
            raise ValueError(f"offset {offset} out of range")
        return self.__data[offset:offset + size]


@register_format("zynq_boot")
class ZynqBootImage(FormatNode):
    WIDTH_DETECTION = 0xaa995566
    IMAGE_ID = 0x584c4e58
    IMAGE_TABLE_VERSION = 0x01020000
    PADDING = 0xffffffff

    BOOT_HEADER = 0x20
    IMAGE_TABLE_OFFSET = 0x98
    REGISTER_INIT = 0xa0
    REGISTER_INIT_COUNT = 256

    ATTRIBUTE_RSA = 1 << 15
    ATTRIBUTE_DESTINATION_SHIFT = 4
    ATTRIBUTE_DESTINATION_MASK = 3

    @classmethod
    def magic(cls, head: bytes) -> bool:
        if len(head) < cls.BOOT_HEADER + 8:
            return False
        width, image_id = struct.unpack_from("<2L", head, cls.BOOT_HEADER)
        return width == cls.WIDTH_DETECTION and image_id == cls.IMAGE_ID

    async def start(self):
        raw = await self._source.read(0, self._source.size)
        if not self.magic(raw):
            raise NoMatch("zynq_boot", "boot header")

        header = struct.unpack_from("<11L", raw, self.BOOT_HEADER)
        if ~sum(header) & 0xffffffff:
            raise ValueError(f"{self.fqdn}: bad boot header checksum")
        (_width, _id, key_source, _version, fsbl_offset, fsbl_size,
         fsbl_load, fsbl_entry, _total, _qspi, _checksum) = header
        if key_source:
            raise NotImplementedError(f"{self.fqdn}: encrypted boot image")

        fsbl = BootImageSlice("fsbl", self._source, fsbl_offset, fsbl_size)
        fsbl.metadata.update(load_address=fsbl_load, entry=fsbl_entry,
                             destination=Destination.PS)
        self.child_add(fsbl)

        self.metadata["register_init"] = [
            (address, value)
            for address, value in struct.iter_unpack(
                "<2L", raw[self.REGISTER_INIT:
                           self.REGISTER_INIT + 8 * self.REGISTER_INIT_COUNT])
            if address != self.PADDING
        ]

        partitions = Node("partition")
        for partition in self.__partitions(raw):
            partitions.child_add(partition)
        self.child_add(partitions)

    def __partitions(self, raw: bytes):
        table, = struct.unpack_from("<L", raw, self.IMAGE_TABLE_OFFSET)
        version, _count, header_word, image_header_word, _ac = \
            struct.unpack_from("<5L", raw, table)
        if version != self.IMAGE_TABLE_VERSION:
            raise ValueError(
                f"{self.fqdn}: unknown image header table version {version:#x}")

        names = self.__image_names(raw, image_header_word)
        offset = header_word * 4
        for index in range(len(raw) // 64):
            name = str(index)
            words = struct.unpack_from("<16L", raw, offset)
            if ~sum(words) & 0xffffffff:
                raise ValueError(
                    f"{self.fqdn}: bad partition header checksum at {offset:#x}")
            if not any(words[:-1]):
                return
            (_encrypted, size, _total, load, entry, data_word, attributes,
             _sections, _checksum, image_header, _ac) = words[:11]
            if attributes & self.ATTRIBUTE_RSA:
                raise NotImplementedError(
                    f"{self.fqdn}: authenticated partition at {offset:#x}")
            destination = Destination(
                (attributes >> self.ATTRIBUTE_DESTINATION_SHIFT)
                & self.ATTRIBUTE_DESTINATION_MASK)
            data_offset, data_size = data_word * 4, size * 4
            if destination == Destination.PL:
                partition = Node(name)
                partition.child_add(BootImageBitstream(
                    "bitstream", raw[data_offset:data_offset + data_size]))
            else:
                partition = BootImageSlice(name, self._source, data_offset, data_size)
            partition.metadata.update(
                name=names.get(image_header, ""), load_address=load,
                entry=entry, destination=destination)
            yield partition
            offset += 64
        raise ValueError(f"{self.fqdn}: unterminated partition header table")

    @staticmethod
    def __image_names(raw: bytes, word: int) -> dict[int, str]:
        """Image names by image header word offset. Names are packed
        four characters per big-endian word."""
        names = {}
        while word:
            offset = word * 4
            next_word, = struct.unpack_from("<L", raw, offset)
            name = b""
            for o in range(offset + 16, len(raw), 4):
                chunk = raw[o:o + 4][::-1]
                name += chunk
                if b"\0" in chunk or chunk == b"\xff" * 4:
                    break
            names[word] = name.split(b"\0")[0].decode("utf-8", "replace")
            word = next_word
        return names


@register_magic
def _zynq_boot_magic(head: bytes):
    return "zynq_boot" if ZynqBootImage.magic(head) else None
