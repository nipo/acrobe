"""Tests for acrobe.vfs.fs — filesystem root Node and FileNode."""

import os

import pytest

from acrobe.db import NoMatch
from acrobe.vfs import FsRoot, FileNode


@pytest.fixture
def tmp_files(tmp_path):
    """Create a small directory tree."""
    (tmp_path / "a.bin").write_bytes(b"hello world")
    (tmp_path / "b.bin").write_bytes(bytes(range(256)))
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.bin").write_bytes(b"nested")
    return tmp_path


class TestFileNode:
    @pytest.mark.asyncio
    async def test_read_full(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("a.bin")
        assert leaf.size == len(b"hello world")
        data = await leaf.read(0, leaf.size)
        assert data == b"hello world"

    @pytest.mark.asyncio
    async def test_read_partial(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("a.bin")
        assert await leaf.read(6, 5) == b"world"
        assert await leaf.read(0, 5) == b"hello"

    @pytest.mark.asyncio
    async def test_pread_short_read_at_eof(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("a.bin")
        # Request more than available
        data = await leaf.read(6, 100)
        assert data == b"world"

    @pytest.mark.asyncio
    async def test_read_zero_at_eof(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("a.bin")
        assert await leaf.read(leaf.size, 10) == b""

    @pytest.mark.asyncio
    async def test_negative_offset_raises(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("a.bin")
        with pytest.raises(ValueError):
            await leaf.read(-1, 10)

    @pytest.mark.asyncio
    async def test_offset_past_size_raises(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("a.bin")
        with pytest.raises(ValueError):
            await leaf.read(leaf.size + 1, 10)

    @pytest.mark.asyncio
    async def test_full_byte_range(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("b.bin")
        assert leaf.size == 256
        data = await leaf.read(0, 256)
        assert data == bytes(range(256))

    @pytest.mark.asyncio
    async def test_metadata(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("a.bin")
        meta = leaf.metadata
        assert meta["size"] == 11
        assert meta["path"].endswith("a.bin")

    @pytest.mark.asyncio
    async def test_stop_closes_file(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        leaf = await root.child_summon("a.bin")
        assert leaf._FileNode__fd is not None
        await leaf.stop_tree()
        assert leaf._FileNode__fd is None


class TestFsRootDirectories:
    @pytest.mark.asyncio
    async def test_subdir_returns_fsroot(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        sub = await root.child_summon("sub")
        assert isinstance(sub, FsRoot)
        nested = await sub.child_summon("nested.bin")
        assert isinstance(nested, FileNode)
        assert await nested.read(0, nested.size) == b"nested"

    @pytest.mark.asyncio
    async def test_unknown_file_raises(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        with pytest.raises(NoMatch):
            await root.child_summon("does-not-exist.bin")

    @pytest.mark.asyncio
    async def test_walk_subdir_in_one_call(self, tmp_files):
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        nested = await root.child_summon("sub", "nested.bin")
        assert isinstance(nested, FileNode)
        assert nested.size == len(b"nested")

    @pytest.mark.asyncio
    async def test_absolute_path(self, tmp_files):
        # Spawning by absolute path also works
        root = FsRoot(str(tmp_files))
        await root.start_tree()
        abspath = str(tmp_files / "a.bin")
        leaf = await root.child_summon(abspath)
        assert isinstance(leaf, FileNode)
        assert await leaf.read(0, leaf.size) == b"hello world"
