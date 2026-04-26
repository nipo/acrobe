"""Tests for Xilinx .bit format Node (component.xilinx.formats)."""

import gzip
import struct

import pytest

import acrobe.component.xilinx.formats  # noqa: F401
from acrobe.vfs import FsRoot
from acrobe.component.xilinx.formats import _HEADER, XilinxBit, XilinxPayload


def _make_section(letter, data):
    return letter + struct.pack(">H", len(data)) + data


def _make_payload_section(data):
    return b"e" + struct.pack(">L", len(data)) + data


def _build_bitstream(*, project="test_project;UserID=0x12345678",
                     device="6slx9tqg144", date="2024/01/15",
                     time="10:30:00", payload=b"\xaa\x99\x55\x66"):
    parts = _HEADER
    parts += _make_section(b"a", project.encode() + b"\x00")
    parts += _make_section(b"b", device.encode() + b"\x00")
    parts += _make_section(b"c", date.encode() + b"\x00")
    parts += _make_section(b"d", time.encode() + b"\x00")
    parts += _make_payload_section(payload)
    return parts


class TestXilinxBit:
    @pytest.mark.asyncio
    async def test_basic_parse(self, tmp_path):
        data = _build_bitstream()
        path = tmp_path / "test.bit"
        path.write_bytes(data)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        bit = await root.child_summon("test.bit")
        assert isinstance(bit.children[0], XilinxPayload)
        assert bit.metadata["device"] == "6slx9tqg144"
        assert bit.metadata["project"] == "test_project"
        assert bit.metadata["userid"] == 0x12345678
        assert "2024/01/15" in bit.metadata["build_date"]
        assert "10:30:00" in bit.metadata["build_date"]

        view = await root.child_summon("test.bit", "bitstream")
        assert (await view.read(0, view.size)) == b"\xaa\x99\x55\x66"

    @pytest.mark.asyncio
    async def test_gzip(self, tmp_path):
        data = _build_bitstream()
        path = tmp_path / "test.bit.gz"
        path.write_bytes(gzip.compress(data))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        bit = await root.child_summon("test.bit.gz")
        assert bit.metadata["userid"] == 0x12345678
        assert bit.metadata.get("gzipped") is True
        view = await root.child_summon("test.bit.gz", "bitstream")
        assert (await view.read(0, view.size)) == b"\xaa\x99\x55\x66"

    @pytest.mark.asyncio
    async def test_no_userid(self, tmp_path):
        data = _build_bitstream(project="my_design")
        path = tmp_path / "test.bit"
        path.write_bytes(data)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        bit = await root.child_summon("test.bit")
        assert "userid" not in bit.metadata
        assert bit.metadata["project"] == "my_design"

    @pytest.mark.asyncio
    async def test_bad_header(self, tmp_path):
        path = tmp_path / "test.bit"
        path.write_bytes(b"\x00" * 20)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(Exception):
            await root.child_summon("test.bit")

    @pytest.mark.asyncio
    async def test_truncated_payload(self, tmp_path):
        data = _build_bitstream()
        path = tmp_path / "test.bit"
        path.write_bytes(data[:-2])
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(ValueError, match="short payload"):
            await root.child_summon("test.bit")

    @pytest.mark.asyncio
    async def test_no_payload(self, tmp_path):
        parts = _HEADER + _make_section(b"a", b"x\x00")
        path = tmp_path / "test.bit"
        path.write_bytes(parts)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(ValueError, match="no bitstream payload"):
            await root.child_summon("test.bit")

    @pytest.mark.asyncio
    async def test_large_payload(self, tmp_path):
        payload = bytes(range(256)) * 100
        data = _build_bitstream(payload=payload)
        path = tmp_path / "test.bit"
        path.write_bytes(data)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon("test.bit", "bitstream")
        assert view.size == len(payload)
        assert (await view.read(0, view.size)) == payload

    @pytest.mark.asyncio
    async def test_via_as(self, tmp_path):
        data = _build_bitstream()
        path = tmp_path / "raw"
        path.write_bytes(data)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        # Even though .bit isn't on the filename, magic detection
        # picks it up. Verify explicit `as(type=xilinx_bit)` also works.
        view = await root.child_summon(
            "raw", "as(type=xilinx_bit)", "bitstream")
        assert (await view.read(0, view.size)) == b"\xaa\x99\x55\x66"
