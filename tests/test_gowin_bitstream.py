"""Tests for Gowin .fs format Node (component.gowin.formats)."""

import gzip

import pytest

import acrobe.component.gowin.formats  # noqa: F401
from acrobe.vfs import FsRoot
from acrobe.component.gowin.formats import GowinFs, GowinPayload


def _make_fs(*, header_lines=None, bits="1010", usercode="0xDEADBEEF"):
    lines = []
    if header_lines is None:
        lines.append("//Device: GW5A-60\n")
        if usercode is not None:
            lines.append(f"//UserCode: {usercode}\n")
        lines.append("//Checksum: 0x1234\n")
    else:
        lines.extend(header_lines)
    lines.append(bits + "\n")
    return "".join(lines)


class TestGowinFs:
    @pytest.mark.asyncio
    async def test_basic_parse(self, tmp_path):
        path = tmp_path / "test.fs"
        path.write_text(_make_fs(bits="10110100"))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        fs = await root.child_summon("test.fs")
        assert fs.metadata["Device"] == "GW5A-60"
        assert fs.metadata["UserCode"] == "0xDEADBEEF"
        assert fs.metadata["Checksum"] == "0x1234"
        view = await root.child_summon("test.fs", "bitstream")
        assert isinstance(view, GowinPayload)
        assert view.size == 1

    @pytest.mark.asyncio
    async def test_multiline_bits(self, tmp_path):
        content = "//Device: t\n10110100\n11001010\n"
        path = tmp_path / "test.fs"
        path.write_text(content)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon("test.fs", "bitstream")
        assert view.size == 2

    @pytest.mark.asyncio
    async def test_gzip(self, tmp_path):
        path = tmp_path / "test.fs.gz"
        path.write_bytes(gzip.compress(_make_fs(bits="10101010").encode()))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        fs = await root.child_summon("test.fs.gz")
        assert fs.metadata["Device"] == "GW5A-60"
        assert fs.metadata.get("gzipped") is True

    @pytest.mark.asyncio
    async def test_missing_usercode(self, tmp_path):
        path = tmp_path / "test.fs"
        path.write_text(_make_fs(usercode=None))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        fs = await root.child_summon("test.fs")
        assert "UserCode" not in fs.metadata

    @pytest.mark.asyncio
    async def test_empty_bitstream_raises(self, tmp_path):
        path = tmp_path / "test.fs"
        path.write_text("//Device: t\n")
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(Exception):
            await root.child_summon("test.fs")

    @pytest.mark.asyncio
    async def test_comment_without_colon_skipped(self, tmp_path):
        content = "//Device: t\n//Just a comment\n10101010\n"
        path = tmp_path / "test.fs"
        path.write_text(content)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        fs = await root.child_summon("test.fs")
        assert fs.metadata["Device"] == "t"
