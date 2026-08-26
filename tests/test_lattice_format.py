"""Tests for Lattice bitstream format Node (component.lattice.formats)."""

import pytest

import acrobe.component.lattice.formats  # noqa: F401
from acrobe.util.endian import bitswap8
from acrobe.vfs import FsRoot
from acrobe.component.lattice.formats import (
    HEADER, HEADER_END, SYNC, SYNC_SWAPPED,
    LatticeBit, LatticePayload,
)


def _build_with_header(metadata: dict, payload_after_sync: bytes):
    fields = b""
    for k, v in metadata.items():
        fields += f"{k}: {v}".encode() + b"\x00"
    return HEADER + fields + HEADER_END + SYNC + payload_after_sync


def _build_no_header(prefix_len: int, payload_after_sync: bytes,
                     swapped: bool = False):
    """Build a blob with a sync word at offset prefix_len.

    When swapped=True, the on-disk sync is SYNC_SWAPPED; the parser
    detects that and bit-swaps the whole blob on read."""
    prefix = b"\x00" * prefix_len
    if swapped:
        return prefix + SYNC_SWAPPED + payload_after_sync
    return prefix + SYNC + payload_after_sync


class TestLatticeBit:
    @pytest.mark.asyncio
    async def test_with_header(self, tmp_path):
        metadata = {"Device": "ECP5-25", "Build": "2024-01-01"}
        blob = _build_with_header(metadata, b"\xab\xcd\xef" * 10)
        path = tmp_path / "design.bin"
        path.write_bytes(blob)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("design.bin")
        assert leaf.metadata["Device"] == "ECP5-25"
        assert leaf.metadata["Build"] == "2024-01-01"
        view = await root.child_summon("design.bin", "bitstream")
        assert isinstance(view, LatticePayload)
        # First bytes are the sync word (no header in payload)
        assert (await view.read(0, len(SYNC))) == SYNC

    @pytest.mark.asyncio
    async def test_no_header_swapped(self, tmp_path):
        # Random data + bit-swapped sync somewhere in the head.
        payload = bytes(range(64))
        blob = _build_no_header(50, payload, swapped=True)
        path = tmp_path / "swap.bin"
        path.write_bytes(blob)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("swap.bin")
        assert leaf.metadata["swapped"] is True
        view = await root.child_summon("swap.bin", "bitstream")
        # First bytes after swap are the original (un-swapped) sync.
        assert (await view.read(0, len(SYNC))) == SYNC

    @pytest.mark.asyncio
    async def test_no_sync_falls_through_to_generic(self, tmp_path):
        # Random bytes with no sync. Lattice magic returns None;
        # ext_db for .bin still resolves to a different parser
        # eventually (Step 11). For now, no auto-detection means
        # the FileNode just stays raw.
        path = tmp_path / "random.bin"
        path.write_bytes(b"\x00\x01\x02\x03" * 256)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("random.bin")
        # No bitstream child auto-populated.
        assert all(c.name != "bitstream" for c in leaf.children)
