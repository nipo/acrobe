"""Lattice bitstream formats as VFS Nodes.

Two unrelated encodings share this file because both ship as `.bin`:

* The ECP5/MachXO family bitstream, identified by a sync word
  (0xFFFFBDB3FFFF) somewhere in the first 1 KiB, with an optional
  ASCII header before the sync carrying metadata.
* The iCE40 configuration bitstream, identified by the 0x7EAA997E
  preamble, optionally preceded by a comment block.

Both are detected via magic only — the .bin extension is too generic
and keeps its generic Bin meaning by default.

Path: `file.bin/bitstream` — preamble-aligned payload.
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
    """Lattice bitstream bytes, aligned on the format's sync word and
    bit-swapped when the source stored them in the other bit order.

    Held in memory because the source may need bit-swapping over its
    entire content; doing it lazily on every read would be wasteful.
    """

    def __init__(self, name, data: bytes):
        super().__init__(name)
        self.__data = data

    @property
    def size(self) -> int:
        return len(self.__data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self.__data):
            raise ValueError(f"offset {offset} out of range")
        avail = len(self.__data) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return self.__data[offset:offset + n]


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

        self.metadata.update(info)
        self.metadata["swapped"] = swapped
        self.metadata["sync_offset"] = sync_off

        view = LatticePayload("bitstream", payload)
        self.child_add(view)


ICE40_SYNC = bytes([0x7e, 0xaa, 0x99, 0x7e])
ICE40_COMMENT_START = b"\xff\x00"
ICE40_COMMENT_END = b"\x00\xff"
ICE40_SCAN = 1024


@register_format("ice40_bit",
                 mimes=["application/x-ice40-bitstream"])
class Ice40Bit(FormatNode):
    """iCE40 bitstream parser. Locates the 0x7EAA997E preamble and
    exposes everything from it onward as one `bitstream` child.

    The optional comment block that precedes the preamble is decoded
    into metadata; it is not part of the payload, so what the device
    gets starts on the preamble whatever the tool wrote in front.
    """

    async def start(self):
        head = await self._source.read(0, min(self._source.size, ICE40_SCAN))

        sync_off = head.find(ICE40_SYNC)
        if sync_off < 0:
            raise NoMatch("ice40_bit", "no preamble")
        if sync_off and not head.startswith(ICE40_COMMENT_START):
            raise NoMatch("ice40_bit", "preamble is not at the start of a "
                                       "file nor behind a comment block")

        blob = await self._source.read(0, self._source.size)

        self.metadata.update(self.__comment_fields(head[:sync_off]))
        self.metadata["sync_offset"] = sync_off

        self.child_add(LatticePayload("bitstream", blob[sync_off:]))

    @staticmethod
    def __comment_fields(prefix: bytes) -> dict:
        """Decode the `key: value` entries of a comment block. Tools
        separate entries with NULs or newlines and are free to write
        free text, which is kept whole under `comment`."""
        if not prefix.startswith(ICE40_COMMENT_START):
            return {}
        body = prefix[len(ICE40_COMMENT_START):]
        if body.endswith(ICE40_COMMENT_END):
            body = body[:-len(ICE40_COMMENT_END)]
        info = {}
        text = []
        for field in body.replace(b"\n", b"\x00").split(b"\x00"):
            entry = field.decode("ascii", "replace").strip()
            if not entry:
                continue
            if ": " in entry:
                k, v = entry.split(": ", 1)
                info[k] = v
            else:
                text.append(entry)
        if text:
            info["comment"] = " ".join(text)
        return info


@register_magic
def _lattice_magic(head: bytes):
    # Match either the ASCII header or the sync word in the first KB.
    if head.startswith(HEADER):
        return "lattice_bit"
    if SYNC in head[:1024] or SYNC_SWAPPED in head[:1024]:
        return "lattice_bit"
    return None


@register_magic
def _ice40_magic(head: bytes):
    sync_off = head.find(ICE40_SYNC, 0, ICE40_SCAN)
    if sync_off < 0:
        return None
    if sync_off == 0 or head.startswith(ICE40_COMMENT_START):
        return "ice40_bit"
    return None
