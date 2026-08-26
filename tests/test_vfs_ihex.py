"""Tests for ihex VFS Node."""

import pytest

from acrobe.vfs import FsRoot
from acrobe.vfs.ihex import Ihex, IhexRegion


def _build_ihex_records(*records):
    """Each record: (rtype, address_lo, data_bytes).

    Returns text with checksums."""
    out = []
    for rtype, address, data in records:
        body = bytes([len(data),
                      (address >> 8) & 0xFF,
                      address & 0xFF,
                      rtype]) + data
        checksum = (~sum(body) + 1) & 0xFF
        out.append(":" + (body + bytes([checksum])).hex().upper())
    return "\n".join(out) + "\n"


class TestIhexAutoDetect:
    @pytest.mark.asyncio
    async def test_basic_data_record(self, tmp_path):
        text = _build_ihex_records(
            (0x04, 0, b"\x00\x00"),                 # ext linear addr 0
            (0x00, 0, b"\xDE\xAD\xBE\xEF"),         # data @ 0
            (0x01, 0, b""),                          # EOF
        )
        path = tmp_path / "a.hex"
        path.write_text(text)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        r0 = await root.child_summon("a.hex", "region", "0")
        assert isinstance(r0, IhexRegion)
        assert r0.load_address == 0
        assert r0.size == 4
        assert (await r0.read(0, 4)) == b"\xDE\xAD\xBE\xEF"

    @pytest.mark.asyncio
    async def test_region_children(self, tmp_path):
        text = _build_ihex_records(
            (0x04, 0, b"\x00\x00"),
            (0x00, 0x00, b"\x01\x02"),
            (0x00, 0x10, b"\x03\x04"),
            (0x01, 0, b""),
        )
        path = tmp_path / "a.hex"
        path.write_text(text)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        regs = (await root.child_summon("a.hex", "region")).children
        assert [r.name for r in regs] == ["0", "1"]
        r0 = await root.child_summon("a.hex", "region", "0")
        r1 = await root.child_summon("a.hex", "region", "1")
        assert r0.load_address == 0
        assert (await r0.read(0, r0.size)) == b"\x01\x02"
        assert r1.load_address == 0x10
        assert (await r1.read(0, r1.size)) == b"\x03\x04"

    @pytest.mark.asyncio
    async def test_two_regions_no_merge(self, tmp_path):
        # When records are non-contiguous, two regions are exposed.
        text = _build_ihex_records(
            (0x04, 0, b"\x00\x00"),
            (0x00, 0x00, b"\xAA"),
            (0x00, 0x04, b"\xBB"),
            (0x01, 0, b""),
        )
        path = tmp_path / "gaps.hex"
        path.write_text(text)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        regs = (await root.child_summon("gaps.hex", "region")).children
        assert len(regs) == 2
        r0 = await root.child_summon("gaps.hex", "region", "0")
        r1 = await root.child_summon("gaps.hex", "region", "1")
        assert r0.load_address == 0
        assert (await r0.read(0, r0.size)) == b"\xAA"
        assert r1.load_address == 4
        assert (await r1.read(0, r1.size)) == b"\xBB"

    @pytest.mark.asyncio
    async def test_metadata(self, tmp_path):
        text = _build_ihex_records(
            (0x00, 0x00, b"\xAA"),
            (0x00, 0x10, b"\xBB"),
            (0x05, 0, b"\x00\x00\x10\x00"),  # entry = 0x1000
            (0x01, 0, b""),
        )
        path = tmp_path / "a.hex"
        path.write_text(text)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("a.hex")
        assert leaf.metadata["entry"] == 0x1000
        assert leaf.metadata["region_count"] == 2

    @pytest.mark.asyncio
    async def test_extended_linear_address(self, tmp_path):
        text = _build_ihex_records(
            (0x04, 0, b"\x00\x01"),                 # high = 0x0001 → 0x10000
            (0x00, 0x00, b"\xCC\xDD"),
            (0x01, 0, b""),
        )
        path = tmp_path / "ext.hex"
        path.write_text(text)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        r0 = await root.child_summon("ext.hex", "region", "0")
        assert r0.load_address == 0x10000


class TestIhexAs:
    @pytest.mark.asyncio
    async def test_explicit_as(self, tmp_path):
        text = _build_ihex_records(
            (0x00, 0x100, b"\xAB"),
            (0x01, 0, b""),
        )
        path = tmp_path / "blob"
        path.write_text(text)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        r0 = await root.child_summon(
            "blob", "as(type=ihex)", "region", "0")
        assert r0.size == 1
        assert (await r0.read(0, 1)) == b"\xAB"
