"""Tests for the UF2 VFS Node."""

import struct

import pytest

from acrobe.db import NoMatch
from acrobe.vfs import FsRoot
from acrobe.vfs.uf2 import (
    Uf2, Uf2Region,
    UF2_MAGIC_START0, UF2_MAGIC_START1, UF2_MAGIC_END,
    UF2_FLAG_NOT_MAIN_FLASH, UF2_FLAG_FAMILY_ID_PRESENT,
    UF2_FAMILY_RP2040,
)


def make_block(target_addr, payload, *, flags=0, block_no=0, num_blocks=1,
               file_or_family=0):
    """Build one 512-byte UF2 block."""
    if len(payload) > 256:
        raise ValueError("payload >256")
    header = struct.pack(
        "<IIIIIIII",
        UF2_MAGIC_START0, UF2_MAGIC_START1, flags, target_addr,
        len(payload), block_no, num_blocks, file_or_family)
    middle = payload + bytes(476 - len(payload))
    tail = struct.pack("<I", UF2_MAGIC_END)
    block = header + middle + tail
    assert len(block) == 512
    return block


def make_file(*blocks):
    return b"".join(blocks)


class TestUf2Parse:
    @pytest.mark.asyncio
    async def test_single_block(self, tmp_path):
        data = make_file(make_block(0x10000000, b"\xde\xad\xbe\xef"))
        path = tmp_path / "fw.uf2"
        path.write_bytes(data)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        r0 = await root.child_summon("fw.uf2", "region", "0")
        assert isinstance(r0, Uf2Region)
        assert r0.load_address == 0x10000000
        assert r0.size == 4
        assert (await r0.read(0, 4)) == b"\xde\xad\xbe\xef"

    @pytest.mark.asyncio
    async def test_contiguous_blocks_merge(self, tmp_path):
        a = make_block(0x10000000, b"A" * 256, block_no=0, num_blocks=2)
        b = make_block(0x10000100, b"B" * 256, block_no=1, num_blocks=2)
        path = tmp_path / "fw.uf2"
        path.write_bytes(make_file(a, b))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        regions = (await root.child_summon("fw.uf2", "region")).children
        assert len(regions) == 1
        r0 = await root.child_summon("fw.uf2", "region", "0")
        assert r0.load_address == 0x10000000
        assert r0.size == 512
        assert (await r0.read(0, 256)) == b"A" * 256
        assert (await r0.read(256, 256)) == b"B" * 256

    @pytest.mark.asyncio
    async def test_gap_splits_regions(self, tmp_path):
        a = make_block(0x10000000, b"\xAA" * 16)
        b = make_block(0x10001000, b"\xBB" * 16)
        path = tmp_path / "fw.uf2"
        path.write_bytes(make_file(a, b))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        regions = (await root.child_summon("fw.uf2", "region")).children
        assert [r.load_address for r in regions] == [0x10000000, 0x10001000]

    @pytest.mark.asyncio
    async def test_not_main_flash_skipped(self, tmp_path):
        # Block 0 carries config (NOT_MAIN_FLASH); only block 1 should
        # end up as a region.
        a = make_block(0x10000000, b"\x00" * 16,
                       flags=UF2_FLAG_NOT_MAIN_FLASH)
        b = make_block(0x10000000, b"\x55" * 16)
        path = tmp_path / "fw.uf2"
        path.write_bytes(make_file(a, b))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        regions = (await root.child_summon("fw.uf2", "region")).children
        assert len(regions) == 1
        assert (await regions[0].read(0, 16)) == b"\x55" * 16

    @pytest.mark.asyncio
    async def test_family_id_in_metadata(self, tmp_path):
        a = make_block(
            0x10000000, b"\xAA" * 16,
            flags=UF2_FLAG_FAMILY_ID_PRESENT,
            file_or_family=UF2_FAMILY_RP2040)
        b = make_block(
            0x10000010, b"\xBB" * 16,
            flags=UF2_FLAG_FAMILY_ID_PRESENT,
            file_or_family=UF2_FAMILY_RP2040)
        path = tmp_path / "fw.uf2"
        path.write_bytes(make_file(a, b))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("fw.uf2")
        assert leaf.metadata["family_ids"] == [UF2_FAMILY_RP2040]
        assert leaf.metadata["block_count"] == 2
        assert leaf.metadata["min_address"] == 0x10000000
        assert leaf.metadata["max_address"] == 0x10000020


class TestUf2Reject:
    @pytest.mark.asyncio
    async def test_wrong_size_rejected(self, tmp_path):
        path = tmp_path / "fw.uf2"
        path.write_bytes(b"\x00" * 100)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(NoMatch):
            await root.child_summon("fw.uf2")

    @pytest.mark.asyncio
    async def test_bad_magic_rejected(self, tmp_path):
        block = bytearray(make_block(0, b"\x00" * 4))
        block[0] = 0xFF
        path = tmp_path / "fw.uf2"
        path.write_bytes(bytes(block))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(NoMatch):
            await root.child_summon("fw.uf2")

    @pytest.mark.asyncio
    async def test_payload_too_large_rejected(self, tmp_path):
        block = make_block(0, b"")
        # Mutate payload_size to 300 (>256).
        block = (block[:0x10] + struct.pack("<I", 300)
                 + block[0x14:])
        path = tmp_path / "fw.uf2"
        path.write_bytes(block)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(NoMatch):
            await root.child_summon("fw.uf2")

    @pytest.mark.asyncio
    async def test_all_blocks_skipped_rejected(self, tmp_path):
        block = make_block(0, b"\x00" * 4, flags=UF2_FLAG_NOT_MAIN_FLASH)
        path = tmp_path / "fw.uf2"
        path.write_bytes(block)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        with pytest.raises(NoMatch):
            await root.child_summon("fw.uf2")
