"""Classic RBF (Raw Binary File) format — Cyclone, older Stratix, Arria.

Continuous bitstream framed by the 0x6af7f7f7 sync word. Either
canonical or bit-reversed byte order; the parser picks whichever
appears in the head of the file. The view exposes canonical bytes.
"""

from ....db import NoMatch
from ....node import Node, Readable
from ....util.endian import bitswap8
from ....vfs import FormatNode, register_format


RBF_SYNC = bytes([0x6a, 0xf7, 0xf7, 0xf7])
RBF_SYNC_SWAPPED = bitswap8(RBF_SYNC)


class RbfBitstream(Node, Readable):
    """JTAG-bit-order view of a raw RBF byte stream.

    Detects bitswap from the position of the sync word (0x6af7f7f7).
    If the sync appears bitswapped in the source, every byte read
    is swapped on the way out.
    """

    def __init__(self, name, source, swapped):
        super().__init__(name)
        self.__source = source
        self.__swapped = swapped

    @property
    def size(self) -> int:
        return self.__source.size

    async def read(self, offset, size):
        data = await self.__source.read(offset, size)
        if self.__swapped:
            data = bitswap8(data)
        return data

    @property
    def metadata(self) -> dict:
        return {"swapped": self.__swapped, **self._metadata}


@register_format("altera_rbf",
                 exts=["rbf"],
                 mimes=["application/x-altera-rbf"])
class Rbf(FormatNode):
    """RBF format. Adds a single `bitstream` child exposing the
    canonical-byte-order view of `source`.

    `swap=true|false|auto` can force the swap behaviour; default
    is `auto` (driven by the detected sync). If no known sync is
    present, parsing fails — the caller can either pick the
    right format or pass `swap=` to bypass detection.
    """

    __RBF_SYNCS = [
        (RBF_SYNC, False),
        (RBF_SYNC_SWAPPED, True),
    ]

    def __init__(self, name, source):
        super().__init__(name, source)
        self.__swap_override = None  # None=auto, True/False=forced

    def option_set(self, key, value):
        if key == "swap":
            v = value.lower()
            if v in ("true", "1", "yes"):
                self.__swap_override = True
            elif v in ("false", "0", "no"):
                self.__swap_override = False
            elif v == "auto":
                self.__swap_override = None
            else:
                raise ValueError(
                    f"{self.fqdn}: swap must be true/false/auto, "
                    f"got {value!r}")

    async def start(self):
        if self.__swap_override is not None:
            swapped = self.__swap_override
            family = "user-specified"
        else:
            head = await self._source.read(
                0, min(self._source.size, 4096))
            best = None
            for sync, sw in self.__RBF_SYNCS:
                pos = head.find(sync)
                if pos >= 0 and (best is None or pos < best[0]):
                    best = (pos, sw, sync)
            if best is None:
                raise NoMatch(
                    "altera_rbf",
                    "no recognised sync word in first 4KiB")
            swapped = best[1]
            family = best[2].hex()
        self.metadata["sync_family"] = family
        view = RbfBitstream("bitstream", self._source, swapped)
        self.child_add(view)

# Note: RBF magic detection is unreliable from raw bytes alone —
# the sync word can appear anywhere in the first KB. We don't
# register a generic magic detector for RBF; users either rely on
# the .rbf extension or use `as(type=rbf)` explicitly.
