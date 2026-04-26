"""Tests for live regions as VFS roots — composing file containers
on top of a hardware byte source."""

import pytest

import acrobe.component.altera.formats  # registers POF parser
from acrobe.node import Node, Readable, Writable, Addressable
from acrobe.vfs import populate_format
from acrobe.component.altera.formats import POF_MAGIC, RBF_SYNC_SWAPPED
import struct


class StubFlash(Node, Writable, Addressable):  # Writable extends Readable
    """Minimal stub flash backing a fixed bytes buffer."""

    def __init__(self, name, data: bytes):
        super().__init__(name)
        self._data = bytearray(data)

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def load_address(self) -> int:
        return 0

    async def read(self, offset, size):
        if offset < 0 or offset > len(self._data):
            raise ValueError("offset out of range")
        avail = len(self._data) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return bytes(self._data[offset:offset + n])

    async def write(self, offset, data):
        if offset + len(data) > len(self._data):
            raise ValueError("write past end")
        self._data[offset:offset + len(data)] = data


def _make_section(tag, data, flags=0):
    return bytes([tag, flags]) + struct.pack("<I", len(data)) + data


def _make_pof_blob():
    """Same shape as test_altera_formats.py."""
    from acrobe.component.altera.formats import SofSection
    sections = b""
    sections += _make_section(SofSection.TOOL, b"FakeQuartus\x00")
    sections += _make_section(SofSection.DEVICE, b"EPCQ\x00")
    sections += _make_section(SofSection.DESIGN, b"d\x00")
    # CONFIG_DATA: 12-byte header + bitswapped RBF
    body = (b"\xff" * 50 + RBF_SYNC_SWAPPED + b"\x00" * 50)
    cd = b"\x00" * 12 + body
    sections += _make_section(SofSection.CONFIG_DATA, cd)
    sections += _make_section(SofSection.END, b"")
    header = POF_MAGIC + struct.pack("<II", 1, 0)
    return header + sections


class TestLiveFlashComposition:
    @pytest.mark.asyncio
    async def test_pof_on_flash(self):
        """A flash chip whose contents are a POF: walk into it via
        as(type=altera_pof) and reach a partition."""
        pof_bytes = _make_pof_blob()
        # Pad to 1 MiB to simulate a real flash
        flash_data = pof_bytes + b"\xff" * (1024 * 1024 - len(pof_bytes))
        flash = StubFlash("flash", flash_data)
        await flash.start_tree()

        # `flash/as(type=altera_pof)/partition/0`
        partition = await flash.child_summon(
            "as(type=altera_pof)", "partition", "0")
        # Read first byte of partition (after the 12-byte header)
        first = await partition.read(0, 1)
        assert first == b"\xff"  # pad before sync

    @pytest.mark.asyncio
    async def test_pof_then_rbf_view_on_flash(self):
        """flash/as(type=altera_pof)/partition/0/as(type=altera_rbf)/bitstream
        gives JTAG-bit-order bytes from data living on a live chip."""
        pof_bytes = _make_pof_blob()
        flash_data = pof_bytes + b"\xff" * (1024 * 1024 - len(pof_bytes))
        flash = StubFlash("flash", flash_data)
        await flash.start_tree()

        view = await flash.child_summon(
            "as(type=altera_pof)", "partition", "0",
            "as(type=altera_rbf)", "bitstream")
        # The first 50 bytes (originally pad) are bitswapped 0xff (still 0xff)
        out = await view.read(0, 60)
        # Sync word at offset 50 in the unswapped view
        from acrobe.util.endian import bitswap8
        # The view is bitswapped from source; check sync appears as RBF_SYNC
        from acrobe.component.altera.formats import RBF_SYNC
        assert RBF_SYNC in out
