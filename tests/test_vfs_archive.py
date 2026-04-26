"""Tests for ZIP and tar archive Nodes."""

import io
import tarfile
import zipfile

import pytest

from acrobe.vfs import FsRoot
from acrobe.vfs.zip import ZipArchive, ZipEntry
from acrobe.vfs.tar import TarArchive, TarEntry
from acrobe.vfs._tree import DirectoryNode


def _make_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_tar(entries: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_targz(entries: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --- ZIP ---

class TestZip:
    @pytest.mark.asyncio
    async def test_flat_entries(self, tmp_path):
        z = _make_zip({"a.bin": b"AAA", "b.bin": b"BB"})
        path = tmp_path / "x.zip"
        path.write_bytes(z)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        a = await root.child_summon("x.zip", "a.bin")
        assert isinstance(a, ZipEntry)
        assert (await a.read(0, a.size)) == b"AAA"
        b = await root.child_summon("x.zip", "b.bin")
        assert (await b.read(0, b.size)) == b"BB"

    @pytest.mark.asyncio
    async def test_nested_entries(self, tmp_path):
        z = _make_zip({
            "top.bin": b"top",
            "sub/inner.bin": b"inner",
            "sub/deep/deepest.bin": b"deepest",
        })
        path = tmp_path / "x.zip"
        path.write_bytes(z)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        # Deep walk
        leaf = await root.child_summon(
            "x.zip", "sub", "deep", "deepest.bin")
        assert (await leaf.read(0, leaf.size)) == b"deepest"
        # Synthesised intermediate dirs
        sub = await root.child_summon("x.zip", "sub")
        assert isinstance(sub, DirectoryNode)

    @pytest.mark.asyncio
    async def test_metadata(self, tmp_path):
        z = _make_zip({"a.bin": b"AAA"})
        path = tmp_path / "x.zip"
        path.write_bytes(z)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        archive = await root.child_summon("x.zip")
        assert archive.metadata["entry_count"] == 1
        a = await root.child_summon("x.zip", "a.bin")
        assert a.metadata["uncompressed_size"] == 3

    @pytest.mark.asyncio
    async def test_explicit_as_zip(self, tmp_path):
        z = _make_zip({"hello.bin": b"world"})
        path = tmp_path / "blob"
        path.write_bytes(z)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon(
            "blob", "as(type=zip)", "hello.bin")
        assert (await leaf.read(0, leaf.size)) == b"world"

    @pytest.mark.asyncio
    async def test_via_mime_type(self, tmp_path):
        z = _make_zip({"x.bin": b"x"})
        path = tmp_path / "blob"
        path.write_bytes(z)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon(
            "blob", 'as(mime-type=application/zip)', "x.bin")
        assert (await leaf.read(0, 1)) == b"x"


# --- tar ---

class TestTar:
    @pytest.mark.asyncio
    async def test_flat_entries(self, tmp_path):
        t = _make_tar({"a.bin": b"AAA", "b.bin": b"BB"})
        path = tmp_path / "x.tar"
        path.write_bytes(t)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        a = await root.child_summon("x.tar", "a.bin")
        assert isinstance(a, TarEntry)
        assert (await a.read(0, a.size)) == b"AAA"

    @pytest.mark.asyncio
    async def test_nested_entries(self, tmp_path):
        t = _make_tar({
            "top.bin": b"top",
            "dir/inner.bin": b"inner",
        })
        path = tmp_path / "x.tar"
        path.write_bytes(t)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("x.tar", "dir", "inner.bin")
        assert (await leaf.read(0, leaf.size)) == b"inner"

    @pytest.mark.asyncio
    async def test_targz(self, tmp_path):
        t = _make_targz({"hello.txt": b"world"})
        path = tmp_path / "x.tar.gz"
        path.write_bytes(t)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("x.tar.gz", "hello.txt")
        assert (await leaf.read(0, leaf.size)) == b"world"


# --- Cross-format walk ---

class TestCrossFormatWalk:
    @pytest.mark.asyncio
    async def test_zip_inside_tar(self, tmp_path):
        # tar containing a zip containing a binary
        inner_zip = _make_zip({"inside.bin": b"inside-zip-inside-tar"})
        outer_tar = _make_tar({"archive.zip": inner_zip})
        path = tmp_path / "outer.tar"
        path.write_bytes(outer_tar)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        # Walk: outer.tar / archive.zip / inside.bin
        leaf = await root.child_summon(
            "outer.tar", "archive.zip", "inside.bin")
        assert (await leaf.read(0, leaf.size)) == b"inside-zip-inside-tar"
