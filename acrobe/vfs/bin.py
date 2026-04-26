"""Generic raw-binary blob as a VFS Node.

The Bin format is essentially identity: it exposes the source's
bytes via a `data` child that is Readable + Addressable. The
`load_address` defaults to 0 but can be overridden via the
`offset=...` option on the `as(...)` invocation.

Example:
    file.bin/as(type=bin)/data            -> at load_address 0
    file.bin/as(type=bin,offset=0x1000)/data -> at load_address 0x1000

We do NOT register the .bin extension here because the lattice
bitstream parser also claims .bin via magic detection (see
acrobe/component/lattice/formats.py). Plain .bin files without
lattice magic stay as raw FileNode.
"""

from ..node import Node, Readable, Addressable
from . import FormatNode, register_format


class BinView(Node, Readable, Addressable):
    def __init__(self, name, source, load_address):
        super().__init__(name)
        self._source = source
        self._load_address = load_address

    @property
    def size(self) -> int:
        return self._source.size

    async def read(self, offset, size):
        return await self._source.read(offset, size)

    @property
    def load_address(self) -> int:
        return self._load_address


@register_format("bin",
                 mimes=["application/octet-stream"])
class Bin(FormatNode):
    """Identity format: exposes source bytes as a `data` child
    with optional load_address."""

    def __init__(self, name, source):
        super().__init__(name, source)
        self._offset = 0

    def option_set(self, key, value):
        if key == "offset":
            self._offset = int(value, 0)
            return
        super().option_set(key, value)

    async def start(self):
        self._child_attach(BinView("data", self._source, self._offset))
