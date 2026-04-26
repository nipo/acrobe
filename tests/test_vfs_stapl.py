"""Tests for STAPL VFS Node (acrobe.vfs.stapl)."""

import pytest

import acrobe.vfs.stapl  # noqa: F401  ensure registration
from acrobe.vfs import FsRoot
from acrobe.vfs.stapl import Stapl, StaplBooleanArray


# A minimal STAPL file with a DATA block containing a BOOLEAN array.
SAMPLE_JAM = """\
NOTE "CREATOR" "stapl vfs test";
NOTE "DEVICE" "test";

ACTION CONFIGURE = DO_CONFIG;

DATA blob;
    BOOLEAN J2[32] = $DEADBEEF;
ENDDATA;

PROCEDURE DO_CONFIG USES blob;
    EXIT 0;
ENDPROC;
CRC 0;
"""


@pytest.fixture
def jam_file(tmp_path):
    path = tmp_path / "test.jam"
    path.write_text(SAMPLE_JAM)
    return tmp_path


class TestStaplAutoDetect:
    @pytest.mark.asyncio
    async def test_basic(self, jam_file):
        root = FsRoot(str(jam_file))
        await root.start_tree()
        leaf = await root.child_summon("test.jam")
        assert "var" in [c.name for c in leaf.children]
        assert "DO_CONFIG" in leaf.metadata["procedures"]
        assert "CONFIGURE" in leaf.metadata["actions"]
        # STAPL parser uppercases identifiers
        assert "BLOB" in leaf.metadata["data_blocks"]

    @pytest.mark.asyncio
    async def test_var_exposed(self, jam_file):
        root = FsRoot(str(jam_file))
        await root.start_tree()
        var = await root.child_summon("test.jam", "var", "J2")
        assert isinstance(var, StaplBooleanArray)
        assert var.metadata["bit_count"] == 32
        data = await var.read(0, var.size)
        assert len(data) == var.size
        assert var.size > 0

    @pytest.mark.asyncio
    async def test_metadata_notes(self, jam_file):
        root = FsRoot(str(jam_file))
        await root.start_tree()
        leaf = await root.child_summon("test.jam")
        notes = leaf.metadata["notes"]
        assert notes["CREATOR"] == "stapl vfs test"
        assert notes["DEVICE"] == "test"


class TestStaplViaAs:
    @pytest.mark.asyncio
    async def test_explicit_as(self, jam_file):
        # Saved without .jam extension: explicit as(type=stapl_jam).
        path = jam_file / "renamed_no_ext"
        (jam_file / "test.jam").rename(path)
        root = FsRoot(str(jam_file))
        await root.start_tree()
        leaf = await root.child_summon("renamed_no_ext")
        as_node = await leaf.child_summon("as(type=stapl_jam)")
        var = await as_node.child_summon("var", "J2")
        assert isinstance(var, StaplBooleanArray)
