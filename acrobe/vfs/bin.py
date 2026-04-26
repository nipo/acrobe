"""Generic raw-binary blob and slice as VFS Nodes.

The Bin format exposes its source's bytes via a `data` child that
is Readable + Addressable. The `load_address` defaults to 0 but
can be overridden via the `offset=` option. When the source is
also Writable, the `data` view is Writable too — writes pass
through to the source (Writable propagation, design D7 / D15).

The Slice format is similar but carves a bounded sub-region:
`as(type=slice,offset=0x100,size=0x40)` exposes a `data` child
of size 0x40, mapping reads/writes to `source[0x100..0x140)`.

Examples:
    file.bin/as(type=bin)/data            -> at load_address 0
    file.bin/as(type=bin,offset=0x1000)/data -> at load_address 0x1000
    flash/as(type=slice,offset=0x100,size=0x40)/data
                                          -> writable 64-byte view

We do NOT register the .bin extension here because the lattice
bitstream parser also claims .bin via magic. Plain .bin files
without lattice magic stay as raw FileNode.
"""

from ..node import Node, Readable, Writable, Addressable
from . import FormatNode, register_format


class _BinReadView(Node, Readable, Addressable):
    """Readable view over a (source, offset, size) triple."""

    def __init__(self, name, source, offset, size, load_address):
        super().__init__(name)
        self._source = source
        self._offset = offset
        self._size = size
        self._load_address = load_address

    @property
    def size(self) -> int:
        return self._size

    @property
    def load_address(self) -> int:
        return self._load_address

    async def read(self, offset, size):
        if offset < 0 or offset > self._size:
            raise ValueError(f"offset {offset} out of range")
        avail = self._size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return await self._source.read(self._offset + offset, n)


class _BinWriteView(_BinReadView, Writable):
    """Writable propagation view: when source is Writable, our
    write() forwards to source.write() at the appropriate offset.
    """

    async def write(self, offset, data):
        if offset < 0 or offset + len(data) > self._size:
            raise ValueError(
                f"write [{offset}, {offset + len(data)}) "
                f"out of slice [0, {self._size})")
        await self._source.write(self._offset + offset, data)


def _make_view(name, source, offset, size, load_address):
    """Pick the right view class based on whether the source
    supports Writable."""
    if isinstance(source, Writable):
        return _BinWriteView(name, source, offset, size, load_address)
    return _BinReadView(name, source, offset, size, load_address)


@register_format("bin",
                 mimes=["application/octet-stream"])
class Bin(FormatNode):
    """Identity format. Exposes the entire source as `data`.

    Options:
        offset=N — load_address of the data view (default 0).
    """

    def __init__(self, name, source):
        super().__init__(name, source)
        self._load_address = 0

    def option_set(self, key, value):
        if key == "offset":
            self._load_address = int(value, 0)
            return
        super().option_set(key, value)

    async def start(self):
        self._child_attach(_make_view(
            "data", self._source,
            offset=0, size=self._source.size,
            load_address=self._load_address))


@register_format("slice")
class Slice(FormatNode):
    """Bounded sub-region of source. Required options: offset=, size=.

    Optional: load_address= (defaults to offset).
    """

    def __init__(self, name, source):
        super().__init__(name, source)
        self._offset = None
        self._size = None
        self._load_address = None

    def option_set(self, key, value):
        if key == "offset":
            self._offset = int(value, 0)
            return
        if key == "size":
            self._size = int(value, 0)
            return
        if key == "load_address":
            self._load_address = int(value, 0)
            return
        super().option_set(key, value)

    async def start(self):
        if self._offset is None:
            raise ValueError(f"{self.fqdn}: slice requires offset=")
        if self._size is None:
            raise ValueError(f"{self.fqdn}: slice requires size=")
        if self._offset + self._size > self._source.size:
            raise ValueError(
                f"{self.fqdn}: slice [{self._offset}, "
                f"{self._offset + self._size}) extends past source size "
                f"{self._source.size}")
        load = self._load_address if self._load_address is not None else self._offset
        self._child_attach(_make_view(
            "data", self._source,
            offset=self._offset, size=self._size,
            load_address=load))
