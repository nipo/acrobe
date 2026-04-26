"""Tests for VFS format dispatch — auto-detection + as(...) child."""

import pytest

from acrobe.node import Node, Readable
from acrobe.vfs import (
    FsRoot,
    FormatNode,
    AsNode,
    register_format,
    register_magic,
    auto_detect,
    detect_by_extension,
    detect_by_mime,
    populate_format,
    format_db,
    ext_db,
    mime_db,
)


# --- Stub format used only for tests ---

# Stub leaf: each "section" in our toy format is N bytes named "secN".
# Header: "STUB" magic + section count (1 byte) + per-section sizes
# (1 byte each). Then concatenated section bodies.

STUB_MAGIC = b"STUB"


class StubSection(Node, Readable):
    def __init__(self, name, source, offset, size):
        super().__init__(name)
        self._source = source
        self._offset = offset
        self._size = size

    @property
    def size(self):
        return self._size

    async def read(self, offset, size):
        if offset < 0 or offset > self._size:
            raise ValueError(f"offset {offset} out of range")
        avail = self._size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return await self._source.read(self._offset + offset, n)


@register_format("stubfmt", exts=["stub"], mimes=["application/x-stub"])
class StubParser(FormatNode):
    async def start(self):
        head = await self._source.read(0, 4)
        if head != STUB_MAGIC:
            raise ValueError("not stub")
        rest = await self._source.read(4, self._source.size - 4)
        n = rest[0]
        sizes = list(rest[1:1 + n])
        body_offset = 4 + 1 + n
        cursor = body_offset
        for i, sz in enumerate(sizes):
            child = StubSection(f"sec{i}", self._source, cursor, sz)
            self._child_attach(child)
            cursor += sz
        self._metadata["section_count"] = n
        self._metadata["sizes"] = sizes


@register_magic
def _stub_magic(head):
    if head[:4] == STUB_MAGIC:
        return "stubfmt"
    return None


def _make_stub_bytes(*sections):
    """Encode some byte sections into the stub format."""
    out = bytearray(STUB_MAGIC)
    out.append(len(sections))
    for s in sections:
        out.append(len(s))
    for s in sections:
        out.extend(s)
    return bytes(out)


# --- Tests ---

class TestRegistryDetection:
    def test_detect_by_extension(self):
        assert detect_by_extension("foo.stub") == "stubfmt"
        assert detect_by_extension("FOO.STUB") == "stubfmt"
        assert detect_by_extension("foo.bin") is None

    def test_detect_by_mime(self):
        assert detect_by_mime("application/x-stub") == "stubfmt"
        assert detect_by_mime("application/x-totally-fake") is None


class TestAutoDetect:
    @pytest.mark.asyncio
    async def test_auto_detect_by_extension(self, tmp_path):
        path = tmp_path / "data.stub"
        path.write_bytes(_make_stub_bytes(b"AAA", b"BB", b"C"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("data.stub")
        assert leaf.size > 0
        # Children should be auto-populated
        names = {c.name for c in leaf.children}
        assert names == {"sec0", "sec1", "sec2"}

    @pytest.mark.asyncio
    async def test_auto_detect_by_magic_no_extension(self, tmp_path):
        path = tmp_path / "noext"
        path.write_bytes(_make_stub_bytes(b"AAA", b"BB"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("noext")
        names = {c.name for c in leaf.children}
        assert names == {"sec0", "sec1"}

    @pytest.mark.asyncio
    async def test_auto_detect_skips_non_matching(self, tmp_path):
        path = tmp_path / "plain.bin"
        path.write_bytes(b"random non-stub bytes")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("plain.bin")
        assert leaf.children == []

    @pytest.mark.asyncio
    async def test_section_read_through_source(self, tmp_path):
        path = tmp_path / "data.stub"
        path.write_bytes(_make_stub_bytes(b"hello", b"world"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        sec0 = await root.child_summon("data.stub", "sec0")
        sec1 = await root.child_summon("data.stub", "sec1")
        assert await sec0.read(0, sec0.size) == b"hello"
        assert await sec1.read(0, sec1.size) == b"world"

    @pytest.mark.asyncio
    async def test_metadata_populated(self, tmp_path):
        path = tmp_path / "data.stub"
        path.write_bytes(_make_stub_bytes(b"A", b"BB"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("data.stub")
        assert leaf.metadata["section_count"] == 2
        assert leaf.metadata["sizes"] == [1, 2]


class TestAsExplicit:
    @pytest.mark.asyncio
    async def test_as_type(self, tmp_path):
        # Save stub bytes under a non-recognised name and force
        # interpretation via as(type=stubfmt)
        path = tmp_path / "blob"
        path.write_bytes(_make_stub_bytes(b"foo", b"bar"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("blob")
        # blob is not auto-recognised: stubfmt's magic is "STUB",
        # which IS recognised. So skip auto-recognition by using
        # a non-matching prefix?
        # Actually: auto-detect by magic WILL kick in. So children
        # already exist. Verify that explicit `as` works regardless.
        as_node = await leaf.child_summon("as(type=stubfmt)")
        assert isinstance(as_node, AsNode)
        names = {c.name for c in as_node.children}
        assert names == {"sec0", "sec1"}

    @pytest.mark.asyncio
    async def test_as_mime(self, tmp_path):
        path = tmp_path / "blob.bin"
        path.write_bytes(_make_stub_bytes(b"x"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("blob.bin")
        as_node = await leaf.child_summon(
            "as(mime-type=application/x-stub)")
        names = {c.name for c in as_node.children}
        assert names == {"sec0"}

    @pytest.mark.asyncio
    async def test_as_full_walk(self, tmp_path):
        path = tmp_path / "blob.bin"
        path.write_bytes(_make_stub_bytes(b"hello", b"world"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        sec1 = await root.child_summon(
            "blob.bin", "as(type=stubfmt)", "sec1")
        assert await sec1.read(0, sec1.size) == b"world"

    @pytest.mark.asyncio
    async def test_as_unknown_type_raises(self, tmp_path):
        path = tmp_path / "blob.bin"
        path.write_bytes(_make_stub_bytes(b"x"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("blob.bin")
        with pytest.raises(Exception):
            await leaf.child_summon("as(type=does-not-exist)")

    @pytest.mark.asyncio
    async def test_as_no_type_raises(self, tmp_path):
        path = tmp_path / "blob.bin"
        path.write_bytes(_make_stub_bytes(b"x"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("blob.bin")
        with pytest.raises(ValueError):
            await leaf.child_summon("as")
