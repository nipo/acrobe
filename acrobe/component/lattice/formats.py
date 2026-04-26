"""Lattice bitstream format as a VFS Node.

Migrated from `acrobe.loadable.lattice`. The Lattice format is
identified by a sync word (0xFFFFBDB3FFFF) somewhere in the first
1 KiB. Optional ASCII header before the sync provides metadata.

Detected via magic only — the .bin extension is too generic and
should keep its generic Bin meaning by default. Use the .bin
extension and the lattice magic detector kicks in.

Path: `file.bin/bitstream` — sync-aligned, auto-bitswapped payload.
"""

from ...db import NoMatch
from ...node import Node, Readable
from ...util.endian import bitswap8
from ...vfs import FormatNode, register_format, register_magic


HEADER = b"\xff\x00Lattice Semiconductor Corporation Bitstream\x00"
HEADER_END = b"\x00\xff"
SYNC = bytes([0xff, 0xff, 0xbd, 0xb3, 0xff, 0xff])
SYNC_SWAPPED = bitswap8(SYNC)


class LatticePayload(Node, Readable):
    """Lattice bitstream bytes, sync-aligned and (if needed) bit-swapped.

    Held in memory because the source may need bit-swapping over its
    entire content; doing it lazily on every read would be wasteful.
    """

    def __init__(self, name, data: bytes):
        super().__init__(name)
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self._data):
            raise ValueError(f"offset {offset} out of range")
        avail = len(self._data) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return self._data[offset:offset + n]


@register_format("lattice_bit",
                 mimes=["application/x-lattice-bitstream"])
class LatticeBit(FormatNode):
    """Lattice bitstream parser. Locates the sync word, optionally
    bit-swaps, exposes one `bitstream` child."""

    async def start(self):
        # Read up to first 1 KiB to locate the sync word.
        head_size = min(self._source.size, 1024)
        head = await self._source.read(0, head_size)

        swapped = False
        sync_off = head.find(SYNC)
        if sync_off < 0:
            sync_off = head.find(SYNC_SWAPPED)
            if sync_off < 0:
                raise NoMatch("lattice_bit", "no sync word")
            swapped = True

        # Extract optional ASCII metadata header.
        info = {}
        if head.startswith(HEADER):
            try:
                end = head.index(HEADER_END)
                fields = head[len(HEADER):end].decode("ascii", "replace")
                for field in fields.split("\x00"):
                    if ": " in field:
                        k, v = field.split(": ", 1)
                        info[k] = v
            except ValueError:
                pass
        elif head[:2] == b"\xff\x00":
            try:
                nul = head.index(b"\x00", 2)
                meta = head[2:nul].decode("ascii", "ignore")
                for field in meta.split("\x00"):
                    if ": " in field:
                        k, v = field.split(": ", 1)
                        info[k] = v
            except ValueError:
                pass

        # Pull entire blob, swap, and start at sync_off.
        blob = await self._source.read(0, self._source.size)
        if swapped:
            blob = bitswap8(blob)
        payload = blob[sync_off:]

        self._metadata.update(info)
        self._metadata["swapped"] = swapped
        self._metadata["sync_offset"] = sync_off

        view = LatticePayload("bitstream", payload)
        self._child_attach(view)


@register_magic
def _lattice_magic(head: bytes):
    # Match either the ASCII header or the sync word in the first KB.
    if head.startswith(HEADER):
        return "lattice_bit"
    if SYNC in head[:1024] or SYNC_SWAPPED in head[:1024]:
        return "lattice_bit"
    return None
