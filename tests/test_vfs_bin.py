"""Tests for Bin VFS format Node."""

import pytest

from acrobe.vfs import FsRoot
from acrobe.vfs.bin import Bin, _BinReadView


class TestBinAs:
    @pytest.mark.asyncio
    async def test_basic(self, tmp_path):
        path = tmp_path / "data.raw"
        path.write_bytes(b"\x01\x02\x03\x04")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon(
            "data.raw", "as(type=bin)", "data")
        assert isinstance(view, _BinReadView)
        assert view.load_address == 0
        assert view.size == 4
        assert (await view.read(0, 4)) == b"\x01\x02\x03\x04"

    @pytest.mark.asyncio
    async def test_with_offset(self, tmp_path):
        path = tmp_path / "data.raw"
        path.write_bytes(b"\xAA\xBB")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon(
            "data.raw", "as(type=bin,offset=0x8000)", "data")
        assert view.load_address == 0x8000
        assert view.size == 2
        assert (await view.read(0, 2)) == b"\xAA\xBB"
