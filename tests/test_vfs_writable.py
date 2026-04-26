"""Tests for Writable propagation through Bin/Slice + non-passthrough rejection."""

import pytest

from acrobe.node import Node, Readable, Writable, Addressable
from acrobe.vfs import FsRoot
from acrobe.vfs.bin import _BinReadView, _BinWriteView


class StubFlash(Node, Writable, Addressable):  # Writable extends Readable
    def __init__(self, name, data: bytes):
        super().__init__(name)
        self._data = bytearray(data)

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def load_address(self) -> int:
        return 0

    async def read(self, offset, size):
        if offset < 0 or offset > len(self._data):
            raise ValueError("offset out of range")
        n = min(size, len(self._data) - offset)
        if n <= 0:
            return b""
        return bytes(self._data[offset:offset + n])

    async def write(self, offset, data):
        if offset + len(data) > len(self._data):
            raise ValueError("past end")
        self._data[offset:offset + len(data)] = data


class TestWritableThroughBin:
    @pytest.mark.asyncio
    async def test_bin_view_is_writable_when_source_is(self):
        flash = StubFlash("flash", b"\xff" * 1024)
        await flash.start_tree()
        view = await flash.child_summon("as(type=bin)", "data")
        assert isinstance(view, _BinWriteView)
        await view.write(0x40, b"\xAB\xCD")
        assert (await flash.read(0x40, 2)) == b"\xAB\xCD"

    @pytest.mark.asyncio
    async def test_bin_view_is_readonly_when_source_not_writable(self, tmp_path):
        path = tmp_path / "x.raw"
        path.write_bytes(b"hello")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon("x.raw", "as(type=bin)", "data")
        # FileNode is Readable but not Writable
        assert isinstance(view, _BinReadView)
        assert not isinstance(view, _BinWriteView)


class TestWritableThroughSlice:
    @pytest.mark.asyncio
    async def test_slice_writable_through_flash(self):
        flash = StubFlash("flash", b"\xff" * 1024)
        await flash.start_tree()
        view = await flash.child_summon(
            "as(type=slice,offset=0x40,size=0x10)", "data")
        assert isinstance(view, _BinWriteView)
        assert view.size == 0x10
        # Address inherits from offset by default.
        assert view.load_address == 0x40
        await view.write(0, b"hello")
        # Underlying flash mutated
        assert (await flash.read(0x40, 5)) == b"hello"
        # Original unaffected outside slice
        assert (await flash.read(0x50, 4)) == b"\xff\xff\xff\xff"

    @pytest.mark.asyncio
    async def test_slice_write_past_end_raises(self):
        flash = StubFlash("flash", b"\xff" * 64)
        await flash.start_tree()
        view = await flash.child_summon(
            "as(type=slice,offset=0,size=8)", "data")
        with pytest.raises(ValueError):
            await view.write(0, b"\x00" * 9)

    @pytest.mark.asyncio
    async def test_slice_extends_past_source_raises(self):
        flash = StubFlash("flash", b"\xff" * 16)
        await flash.start_tree()
        with pytest.raises(ValueError):
            await flash.child_summon("as(type=slice,offset=8,size=16)")


class TestNonPassthroughBlocked:
    """ZIP / POF / similar containers transform bytes — they MUST NOT
    propagate Writable. Children of such containers are read-only."""

    @pytest.mark.asyncio
    async def test_zip_entry_not_writable(self, tmp_path):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.bin", b"hello")
        path = tmp_path / "x.zip"
        path.write_bytes(buf.getvalue())
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        entry = await root.child_summon("x.zip", "a.bin")
        assert isinstance(entry, Readable)
        assert not isinstance(entry, Writable)

    @pytest.mark.asyncio
    async def test_pof_partition_not_writable(self, tmp_path):
        # Construct a simple POF file
        import struct
        from acrobe.component.altera.formats import POF_MAGIC, SofSection
        config_data = b"\x00" * 12 + b"payload"
        sections = b""
        for tag, body in [
                (SofSection.TOOL, b"q\x00"),
                (SofSection.DEVICE, b"d\x00"),
                (SofSection.DESIGN, b"x\x00"),
                (SofSection.CONFIG_DATA, config_data),
                (SofSection.END, b""),
        ]:
            sections += bytes([tag, 0]) + struct.pack("<I", len(body)) + body
        pof = POF_MAGIC + struct.pack("<II", 1, 0) + sections
        path = tmp_path / "a.pof"
        path.write_bytes(pof)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        partition = await root.child_summon("a.pof", "partition", "0")
        assert isinstance(partition, Readable)
        assert not isinstance(partition, Writable)
