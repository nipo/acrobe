import os
import tempfile
import pytest
from acrobe.loadable import Segment, Program

# Ensure format parsers are registered
import acrobe.loadable.bin
import acrobe.loadable.ihex


class TestSegment:
    def test_basic(self):
        seg = Segment(0x1000, b"\xaa\xbb\xcc")
        assert seg.address == 0x1000
        assert len(seg) == 3
        assert seg.end == 0x1003

    def test_getitem(self):
        seg = Segment(0, b"\x01\x02\x03")
        assert seg[0] == 1
        assert seg[1:3] == bytearray(b"\x02\x03")

    def test_setitem(self):
        seg = Segment(0, b"\x00\x00\x00")
        seg[1] = 0xff
        assert seg[1] == 0xff

    def test_lt(self):
        a = Segment(0x100, b"\x00")
        b = Segment(0x200, b"\x00")
        assert a < b

    def test_str(self):
        seg = Segment(0x1000, b"\x00" * 256, name="test")
        s = str(seg)
        assert "0x00001000" in s
        assert "256 bytes" in s
        assert "test" in s


class TestProgram:
    def test_empty(self):
        p = Program()
        assert len(p) == 0
        assert p.size == 0

    def test_append_and_iterate(self):
        p = Program()
        p.append(Segment(0, b"\x00"))
        p.append(Segment(0x100, b"\x01"))
        assert len(p) == 2
        assert list(p)[0].address == 0

    def test_segment_at(self):
        p = Program()
        p.append(Segment(0x1000, b"\xaa\xbb"))
        seg = p.segment_at(0x1001)
        assert seg is not None
        assert seg.address == 0x1000

    def test_segment_at_none(self):
        p = Program()
        p.append(Segment(0x1000, b"\xaa"))
        assert p.segment_at(0x2000) is None

    def test_size(self):
        p = Program()
        p.append(Segment(0, b"\x00" * 10))
        p.append(Segment(0x100, b"\x00" * 20))
        assert p.size == 30

    def test_address_and_end(self):
        p = Program()
        p.append(Segment(0x100, b"\x00" * 10))
        p.append(Segment(0x200, b"\x00" * 10))
        assert p.address == 0x100
        assert p.end == 0x20a

    def test_within(self):
        p = Program()
        p.append(Segment(0x100, b"\x01" * 0x100))
        clipped = p.within(0x120, 0x140)
        assert len(clipped) == 1
        assert clipped[0].address == 0x120
        assert len(clipped[0]) == 0x20

    def test_read(self):
        p = Program()
        p.append(Segment(0x10, b"\xaa\xbb\xcc"))
        data = p.read(0x0e, 8)
        assert len(data) == 8
        assert data[2] == 0xaa  # offset 0x10 - 0x0e = 2
        assert data[3] == 0xbb
        assert data[4] == 0xcc
        assert data[0] == 0  # gap filled with 0

    def test_add(self):
        a = Program()
        a.append(Segment(0, b"\x01"))
        b = Program()
        b.append(Segment(0x100, b"\x02"))
        c = a + b
        assert len(c) == 2

    def test_iadd(self):
        a = Program()
        a.append(Segment(0, b"\x01"))
        b = Program()
        b.append(Segment(0x100, b"\x02"))
        a += b
        assert len(a) == 2

    def test_simplified_merges_adjacent(self):
        p = Program()
        p.append(Segment(0, b"\x01\x02"))
        p.append(Segment(2, b"\x03\x04"))
        s = p.simplified()
        assert len(s) == 1
        assert s[0].address == 0
        assert bytes(s[0].data) == b"\x01\x02\x03\x04"

    def test_simplified_merges_overlapping(self):
        p = Program()
        p.append(Segment(0, b"\x01\x02\x03\x04"))
        p.append(Segment(2, b"\xaa\xbb"))
        s = p.simplified()
        assert len(s) == 1
        assert bytes(s[0].data) == b"\x01\x02\xaa\xbb"


class TestPaged:
    def test_basic_paging(self):
        p = Program()
        p.append(Segment(0x105, b"\xaa" * 10))
        paged = p.paged(256)
        assert len(paged) == 1
        assert paged[0].address == 0x100
        assert len(paged[0]) == 256
        # Check data at offset 5
        assert paged[0][5] == 0xaa


class TestBinFormat:
    def test_save_and_load(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            fname = f.name

        try:
            p = Program()
            p.append(Segment(0, b"\xde\xad\xbe\xef"))
            p.save_bin(fname)

            loaded = Program.from_file(fname)
            assert len(loaded) == 1
            assert bytes(loaded[0].data) == b"\xde\xad\xbe\xef"
        finally:
            os.unlink(fname)


class TestIHexFormat:
    def test_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".hex", delete=False, mode="w") as f:
            fname = f.name

        try:
            p = Program()
            p.append(Segment(0x1000, b"\x01\x02\x03\x04\x05\x06\x07\x08"))
            p.save_hex(fname)

            loaded = Program.from_file(fname)
            s = loaded.simplified()
            assert len(s) == 1
            assert s[0].address == 0x1000
            assert bytes(s[0].data) == b"\x01\x02\x03\x04\x05\x06\x07\x08"
        finally:
            os.unlink(fname)

    def test_parse_basic(self):
        hex_content = (
            ":020000040000FA\n"   # Extended linear address: 0x0000
            ":04000000DEADBEEFC4\n"  # Data at 0x0000: DE AD BE EF
            ":00000001FF\n"       # EOF
        )
        with tempfile.NamedTemporaryFile(suffix=".hex", delete=False, mode="w") as f:
            f.write(hex_content)
            fname = f.name

        try:
            p = Program.from_file(fname)
            assert len(p) == 1
            assert bytes(p[0].data) == b"\xde\xad\xbe\xef"
        finally:
            os.unlink(fname)


# ELF parsing has migrated from acrobe.loadable.elf to the VFS in
# acrobe.vfs.elf. See tests/test_vfs_elf.py for the new tests.
