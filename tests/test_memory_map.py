"""Tests for MemoryMap (replaces tests/test_loadable.py)."""

import os
import tempfile

import pytest

from acrobe.memory_map import MemoryMap, save_bin, save_hex, save


class TestBasic:
    def test_empty(self):
        m = MemoryMap()
        assert len(m) == 0
        assert m.size == 0
        assert not m

    def test_append(self):
        m = MemoryMap()
        m.append(0, b"\x00")
        m.append(0x100, b"\x01")
        assert len(m) == 2
        assert m[0] == (0, b"\x00")

    def test_iterate(self):
        m = MemoryMap()
        m.append(0x100, b"AB")
        m.append(0x200, b"CD")
        seen = list(m)
        assert seen == [(0x100, b"AB"), (0x200, b"CD")]

    def test_size_address_end(self):
        m = MemoryMap()
        m.append(0x100, b"\x00" * 10)
        m.append(0x200, b"\x00" * 20)
        assert m.size == 30
        assert m.address == 0x100
        assert m.end == 0x214


class TestWithin:
    def test_clip(self):
        m = MemoryMap()
        m.append(0x100, b"\x01" * 0x100)
        clipped = m.within(0x120, 0x140)
        assert len(clipped) == 1
        addr, data = clipped[0]
        assert addr == 0x120
        assert len(data) == 0x20

    def test_outside(self):
        m = MemoryMap()
        m.append(0x100, b"x" * 16)
        assert len(m.within(0x200, 0x300)) == 0


class TestRead:
    def test_with_gaps_zero_filled(self):
        m = MemoryMap()
        m.append(0x10, b"\xaa\xbb\xcc")
        data = m.read(0x0e, 8)
        assert len(data) == 8
        assert data[2] == 0xaa
        assert data[3] == 0xbb
        assert data[4] == 0xcc
        assert data[0] == 0


class TestSimplified:
    def test_merge_adjacent(self):
        m = MemoryMap()
        m.append(0, b"\x01\x02")
        m.append(2, b"\x03\x04")
        s = m.simplified()
        assert len(s) == 1
        assert s[0] == (0, b"\x01\x02\x03\x04")

    def test_merge_overlapping(self):
        m = MemoryMap()
        m.append(0, b"\x01\x02\x03\x04")
        m.append(2, b"\xaa\xbb")
        s = m.simplified()
        assert len(s) == 1
        assert s[0] == (0, b"\x01\x02\xaa\xbb")


class TestPaged:
    def test_basic(self):
        m = MemoryMap()
        m.append(0x105, b"\xaa" * 10)
        paged = m.paged(256)
        assert len(paged) == 1
        addr, data = paged[0]
        assert addr == 0x100
        assert len(data) == 256
        assert data[5] == 0xaa


class TestAdd:
    def test_add(self):
        a = MemoryMap()
        a.append(0, b"\x01")
        b = MemoryMap()
        b.append(0x100, b"\x02")
        c = a + b
        assert len(c) == 2

    def test_iadd(self):
        a = MemoryMap()
        a.append(0, b"\x01")
        b = MemoryMap()
        b.append(0x100, b"\x02")
        a += b
        assert len(a) == 2


class TestSaveBin:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "out.bin"
        m = MemoryMap()
        m.append(0, b"\xde\xad\xbe\xef")
        save_bin(m, str(path))
        assert path.read_bytes() == b"\xde\xad\xbe\xef"

    def test_multi_chunk_raises(self, tmp_path):
        path = tmp_path / "out.bin"
        m = MemoryMap()
        m.append(0, b"a")
        m.append(0x100, b"b")
        with pytest.raises(ValueError):
            save_bin(m, str(path))


class TestSaveHex:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "out.hex"
        m = MemoryMap()
        m.append(0x1000, b"\x01\x02\x03\x04\x05\x06\x07\x08")
        save_hex(m, str(path))
        # Re-parse via VFS to verify
        from acrobe.vfs import FsRoot
        import asyncio

        async def check():
            root = FsRoot(str(tmp_path))
            await root.start_tree()
            r0 = await root.child_summon("out.hex", "region", "0")
            assert r0.load_address == 0x1000
            data = await r0.read(0, r0.size)
            assert data == b"\x01\x02\x03\x04\x05\x06\x07\x08"

        asyncio.run(check())


class TestSaveDispatch:
    def test_save_picks_format(self, tmp_path):
        path = tmp_path / "out.bin"
        m = MemoryMap()
        m.append(0, b"x")
        save(m, str(path))
        assert path.read_bytes() == b"x"

        path2 = tmp_path / "out.hex"
        save(m, str(path2))
        text = path2.read_text()
        assert text.startswith(":")  # ihex format
