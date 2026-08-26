"""Tests for synthesised literal VFS Nodes."""

import pytest

from acrobe.vfs import FsRoot
from acrobe.vfs.literals import _LiteralBytes


class TestLiterals:
    @pytest.mark.asyncio
    async def test_literal_value(self, tmp_path):
        # `as` requires a Readable parent. Use any file as the
        # source; literals ignore source content.
        (tmp_path / "anchor").write_bytes(b"")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon(
            "anchor", "as(type=literal,value=DEADBEEF)", "data")
        assert isinstance(leaf, _LiteralBytes)
        assert leaf.size == 4
        assert (await leaf.read(0, 4)) == b"\xde\xad\xbe\xef"

    @pytest.mark.asyncio
    async def test_zero(self, tmp_path):
        (tmp_path / "anchor").write_bytes(b"")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon(
            "anchor", "as(type=zero,size=64)", "data")
        assert leaf.size == 64
        assert (await leaf.read(0, 64)) == b"\x00" * 64

    @pytest.mark.asyncio
    async def test_one(self, tmp_path):
        (tmp_path / "anchor").write_bytes(b"")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon(
            "anchor", "as(type=one,size=0x10)", "data")
        assert leaf.size == 16
        assert (await leaf.read(0, 16)) == b"\xff" * 16

    @pytest.mark.asyncio
    async def test_random(self, tmp_path):
        (tmp_path / "anchor").write_bytes(b"")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon(
            "anchor", "as(type=random,size=128)", "data")
        assert leaf.size == 128
        # Just check it's not all zeros (probabilistically true)
        data = await leaf.read(0, 128)
        assert data != b"\x00" * 128

    @pytest.mark.asyncio
    async def test_missing_value_raises(self, tmp_path):
        (tmp_path / "anchor").write_bytes(b"")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(ValueError):
            await root.child_summon(
                "anchor", "as(type=literal)")
