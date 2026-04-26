"""Tests for Altera POF/SOF/RBF format Nodes (component.altera.formats)."""

import gzip
import struct

import pytest

# Import to trigger format registrations
import acrobe.component.altera.formats  # noqa: F401
from acrobe.util.endian import bitswap8
from acrobe.vfs import FsRoot
from acrobe.component.altera.formats import (
    POF_MAGIC, SOF_MAGIC, RBF_SYNC, RBF_SYNC_SWAPPED,
    SofSection, Pof, Sof, Rbf, PofPartition, RbfBitstream,
)


def _make_section(tag, data, flags=0):
    """Encode one POF/SOF section: tag(1) flags(1) length(4) data."""
    return bytes([tag, flags]) + struct.pack("<I", len(data)) + data


def _make_pof(*, tool="QuartusFakerstu",
              flash="EPCQ256",
              design="my_design",
              partitions=None,
              partition_data=None):
    """Build a minimal POF blob.

    partitions: list of (name, address, size) — BOOT_INFO entries.
    partition_data: dict[address] -> bytes — bytes within CONFIG_DATA.
    """
    if partitions is None:
        partitions = []
    if partition_data is None:
        partition_data = {}

    # Build CONFIG_DATA: 12-byte header + concatenated partition bytes.
    if not partitions:
        config_data = b"\x00" * 12 + b"raw_bitstream_data"
    else:
        # Total size needed
        total = max(addr + size for _, addr, size in partitions)
        cd = bytearray(b"\x00" * total)
        for addr, blob in partition_data.items():
            cd[addr:addr + len(blob)] = blob
        config_data = bytes(cd)

    sections = b""
    sections += _make_section(SofSection.TOOL, tool.encode() + b"\x00")
    sections += _make_section(SofSection.DEVICE, flash.encode() + b"\x00")
    sections += _make_section(SofSection.DESIGN, design.encode() + b"\x00")
    sections += _make_section(SofSection.CONFIG_DATA, config_data)
    if partitions:
        boot_info = ";".join(
            f"{n} {a:x} {s:x}" for n, a, s in partitions) + "\x00"
        sections += _make_section(SofSection.BOOT_INFO, boot_info.encode())
    sections += _make_section(SofSection.END, b"")

    header = POF_MAGIC + struct.pack("<II", 1, 0)
    return header + sections


def _make_sof(*, tool="QuartusFakerstu", device="EP4CE", design="d"):
    config_data = b"\xab" * 100
    sections = b""
    sections += _make_section(SofSection.TOOL, tool.encode() + b"\x00")
    sections += _make_section(SofSection.DEVICE, device.encode() + b"\x00")
    sections += _make_section(SofSection.DESIGN, design.encode() + b"\x00")
    sections += _make_section(SofSection.CONFIG_DATA, config_data)
    sections += _make_section(SofSection.END, b"")
    header = SOF_MAGIC + struct.pack("<II", 1, 0)
    return header + sections


# --- POF auto-detect ---

class TestPofAutoDetect:
    @pytest.mark.asyncio
    async def test_no_boot_info_single_partition(self, tmp_path):
        # POF without BOOT_INFO falls back to one partition over
        # CONFIG_DATA[12:].
        pof = _make_pof(partitions=[])
        path = tmp_path / "a.pof"
        path.write_bytes(pof)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("a.pof", "partition", "0")
        assert isinstance(leaf, PofPartition)
        data = await leaf.read(0, leaf.size)
        assert data == b"raw_bitstream_data"

    @pytest.mark.asyncio
    async def test_metadata(self, tmp_path):
        pof = _make_pof()
        path = tmp_path / "a.pof"
        path.write_bytes(pof)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("a.pof")
        assert leaf.metadata["tool"] == "QuartusFakerstu"
        assert leaf.metadata["flash"] == "EPCQ256"
        assert leaf.metadata["design"] == "my_design"

    @pytest.mark.asyncio
    async def test_multi_partition(self, tmp_path):
        # Three partitions in BOOT_INFO. CONFIG_DATA layout:
        # [0..0x100): partition A
        # [0x100..0x200): partition B
        # [0x200..0x280): partition C
        partitions = [("A", 0x000, 0x100),
                      ("B", 0x100, 0x100),
                      ("C", 0x200, 0x080)]
        pof = _make_pof(
            partitions=partitions,
            partition_data={
                0x000: b"AAAA" + b"\x01" * 0xfc,
                0x100: b"BBBB" + b"\x02" * 0xfc,
                0x200: b"CCCC" + b"\x03" * 0x7c,
            },
        )
        path = tmp_path / "multi.pof"
        path.write_bytes(pof)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        names = [c.name for c in (
            await root.child_summon("multi.pof", "partition")).children]
        assert names == ["0", "1", "2"]

        # Check each partition's first 4 bytes.
        p0 = await root.child_summon("multi.pof", "partition", "0")
        assert (await p0.read(0, 4)) == b"AAAA"
        p1 = await root.child_summon("multi.pof", "partition", "1")
        assert (await p1.read(0, 4)) == b"BBBB"
        p2 = await root.child_summon("multi.pof", "partition", "2")
        assert (await p2.read(0, 4)) == b"CCCC"

    @pytest.mark.asyncio
    async def test_partition_addressable(self, tmp_path):
        partitions = [("only", 0x40, 0x20)]
        pof = _make_pof(
            partitions=partitions,
            partition_data={0x40: b"X" * 0x20},
        )
        path = tmp_path / "a.pof"
        path.write_bytes(pof)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        p = await root.child_summon("a.pof", "partition", "0")
        # load_address is the absolute offset within source (POF blob).
        # That's CONFIG_DATA's offset within the POF + partition's
        # relative address.
        assert isinstance(p, PofPartition)
        assert p.load_address > 0  # known finite, depends on header sizes
        assert p.size == 0x20


# --- POF via as(type=pof) on a raw blob ---

class TestPofAs:
    @pytest.mark.asyncio
    async def test_as_type_pof(self, tmp_path):
        pof = _make_pof()
        # Save under a non-recognized name so .pof auto-detect doesn't
        # fire on the FileNode (we test the explicit `as` path).
        path = tmp_path / "raw_blob"
        path.write_bytes(pof)
        # But our magic detector for POF will fire too. Let's use
        # a clearly non-POF wrapper... actually since our magic detector
        # scans the head, an actual POF will be detected. To genuinely
        # exercise the explicit `as(type=pof)` path, prepend some
        # garbage and let the user specify the offset. But we don't
        # support offset slicing yet — skip and accept that auto-detect
        # will also fire here. Test that explicit access still works.
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("raw_blob")
        as_node = await leaf.child_summon("as(type=altera_pof)")
        # AsNode children include the partition container.
        assert any(c.name == "partition" for c in as_node.children)


# --- SOF ---

class TestSofAutoDetect:
    @pytest.mark.asyncio
    async def test_basic(self, tmp_path):
        sof = _make_sof()
        path = tmp_path / "a.sof"
        path.write_bytes(sof)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("a.sof")
        assert leaf.metadata["tool"] == "QuartusFakerstu"
        assert leaf.metadata["device"] == "EP4CE"
        assert leaf.metadata["design"] == "d"
        cd = await root.child_summon("a.sof", "config_data")
        assert cd.size == 100
        data = await cd.read(0, 100)
        assert data == b"\xab" * 100

    @pytest.mark.asyncio
    async def test_gzipped_sof(self, tmp_path):
        sof = _make_sof()
        path = tmp_path / "a.sof"
        path.write_bytes(gzip.compress(sof))
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("a.sof")
        assert leaf.metadata.get("gzipped") is True
        cd = await root.child_summon("a.sof", "config_data")
        assert cd.size == 100


# --- RBF ---

class TestRbf:
    @pytest.mark.asyncio
    async def test_rbf_normal(self, tmp_path):
        # RBF with sync word at offset 100, no bitswap.
        body = b"\xff" * 100 + RBF_SYNC + b"\xab\xcd" * 50
        path = tmp_path / "design.rbf"
        path.write_bytes(body)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon("design.rbf", "bitstream")
        assert isinstance(view, RbfBitstream)
        assert view.size == len(body)
        out = await view.read(0, view.size)
        assert out == body  # not swapped

    @pytest.mark.asyncio
    async def test_rbf_swapped(self, tmp_path):
        # RBF stored bit-reversed (rare but supported).
        body = b"\xff" * 100 + RBF_SYNC_SWAPPED + b"\x00" * 100
        path = tmp_path / "swapped.rbf"
        path.write_bytes(body)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon("swapped.rbf", "bitstream")
        out = await view.read(0, view.size)
        assert out == bitswap8(body)

    @pytest.mark.asyncio
    async def test_rbf_no_sync_passthrough(self, tmp_path):
        # Bytes without any classic sync word: parser passes through
        # (Agilex/SDM-style RBFs don't carry the legacy sync). Caller
        # asserted "this is RBF" by file extension or as(type=...).
        body = b"\xab" * 1024
        path = tmp_path / "modern.rbf"
        path.write_bytes(body)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon("modern.rbf", "bitstream")
        assert view.size == len(body)
        assert (await view.read(0, view.size)) == body
        assert view._swapped is False

    @pytest.mark.asyncio
    async def test_rbf_explicit_swap_override(self, tmp_path):
        body = b"\x12\x34\x56\x78"
        path = tmp_path / "blob"
        path.write_bytes(body)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        view = await root.child_summon(
            "blob", "as(type=altera_rbf,swap=true)", "bitstream")
        assert view._swapped is True
        # Output is bit-reversed
        assert (await view.read(0, 4)) == bitswap8(body)


# --- Composition: POF / partition / as(type=rbf) / bitstream ---

class TestPofPartitionAsRbf:
    @pytest.mark.asyncio
    async def test_partition_to_rbf_view(self, tmp_path):
        # Build a POF whose single partition contains bitswapped RBF.
        rbf_payload = (b"\xff" * 50 + RBF_SYNC_SWAPPED + b"\x55" * 50)
        # Layout the partition starting at CONFIG_DATA address 0x40,
        # size matches payload.
        partitions = [("MAIN", 0x40, len(rbf_payload))]
        pof = _make_pof(
            partitions=partitions,
            partition_data={0x40: rbf_payload},
        )
        path = tmp_path / "a.pof"
        path.write_bytes(pof)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        # Walk: partition/0 / as(type=altera_rbf) / bitstream
        view = await root.child_summon(
            "a.pof", "partition", "0",
            "as(type=altera_rbf)", "bitstream")
        assert isinstance(view, RbfBitstream)
        out = await view.read(0, view.size)
        # Output should be unswapped (JTAG-bit-order).
        assert out == bitswap8(rbf_payload)
