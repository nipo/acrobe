"""POF (Programmer Object File) format parser.

POF is a flash-image container. It carries tool/design/flash strings
plus a BOOT_INFO list describing partitions inside the embedded flash
image (CONFIG_DATA section).
"""

from ....db import NoMatch
from ....node import Node, Readable, Addressable
from ....vfs import (
    FormatNode,
    register_format,
    register_magic,
)
from .sof import SofSection, _parse_sections, _decode_string


POF_MAGIC = b"POF\x00"


def _parse_boot_info(data: bytes):
    """Parse BOOT_INFO string into partition entries.

    Format: "NAME ADDR SIZE;NAME ADDR SIZE;..."
    Returns list of (name, address, size).
    """
    text = _decode_string(data)
    partitions = []
    for entry in text.split(";"):
        tokens = entry.split()
        if len(tokens) >= 3:
            try:
                name = tokens[0]
                addr = int(tokens[1], 16)
                size = int(tokens[2], 16)
                partitions.append((name, addr, size))
            except ValueError:
                continue
    return partitions


class PofPartition(Node, Readable, Addressable):
    """One partition inside a POF flash image.

    Bytes are read directly from the source (the POF's parent
    Readable) at the partition's flash offset.
    """

    def __init__(self, name, source, flash_address, size):
        super().__init__(name)
        self._source = source
        self._flash_address = flash_address
        self._size = size

    @property
    def size(self) -> int:
        return self._size

    @property
    def load_address(self) -> int:
        return self._flash_address

    async def read(self, offset, size):
        if offset < 0 or offset > self._size:
            raise ValueError(f"offset {offset} out of range")
        avail = self._size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return await self._source.read(self._flash_address + offset, n)

    @property
    def metadata(self) -> dict:
        return {
            "flash_address": self._flash_address,
            "size": self._size,
            **self._metadata,
        }


@register_format("altera_pof",
                 exts=["pof"],
                 mimes=["application/x-altera-pof"])
class Pof(FormatNode):
    """POF flash-image container.

    POF stores tool/design/flash strings in TLV sections, plus a
    BOOT_INFO section listing partitions inside the embedded flash
    image (CONFIG_DATA section). Each partition becomes a
    PofPartition child whose read() pulls bytes from the source's
    CONFIG_DATA region.
    """

    def __init__(self, name, source):
        super().__init__(name, source)
        self.tool = None
        self.flash = None
        self.design = None

    async def start(self):
        blob = await self._source.read(0, self._source.size)
        if blob[:4] != POF_MAGIC:
            raise NoMatch("altera_pof", "magic")

        sections = list(_parse_sections(blob))

        config_offset = None
        config_size = None
        boot_info = None
        idx = 12
        for tag, flags, data in sections:
            if tag == SofSection.CONFIG_DATA:
                config_offset = idx + 6
                config_size = len(data)
            if tag == SofSection.BOOT_INFO:
                boot_info = _parse_boot_info(data)
            if tag == SofSection.TOOL:
                self.tool = _decode_string(data)
            if tag == SofSection.DEVICE:
                self.flash = _decode_string(data)
            if tag == SofSection.DESIGN:
                self.design = _decode_string(data)
            idx += 6 + len(data)

        if config_offset is None:
            raise ValueError(f"{self.fqdn}: POF has no CONFIG_DATA section")

        self._metadata.update({
            "tool": self.tool,
            "flash": self.flash,
            "design": self.design,
            "config_data_offset": config_offset,
            "config_data_size": config_size,
        })

        if boot_info is None:
            partition = PofPartition(
                "0", self._source,
                flash_address=config_offset + 12,
                size=config_size - 12,
            )
            container = self._make_partition_container([partition])
            self._child_attach(container)
            return

        sorted_bi = sorted(boot_info, key=lambda p: p[1])
        partitions = []
        for i, (pname, paddr, psize) in enumerate(sorted_bi):
            if i + 1 < len(sorted_bi):
                region_end = min(paddr + psize, sorted_bi[i + 1][1])
            else:
                region_end = min(paddr + psize, config_size)
            region_size = region_end - paddr
            if region_size <= 0:
                continue
            partition = PofPartition(
                str(i), self._source,
                flash_address=config_offset + paddr,
                size=region_size,
            )
            partition._metadata["partition_name"] = pname
            partitions.append(partition)

        if not partitions:
            raise ValueError(f"{self.fqdn}: POF BOOT_INFO has no partitions")

        container = self._make_partition_container(partitions)
        self._child_attach(container)

    def _make_partition_container(self, partitions):
        container = Node("partition")
        for p in partitions:
            container._child_attach(p)
        return container


@register_magic
def _pof_magic(head: bytes):
    if head[:4] == POF_MAGIC:
        return "altera_pof"
    return None
