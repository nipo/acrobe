import struct

import pytest

from acrobe.component.cypress.boot_image import Fx3BootImage
from acrobe.memory_map import MemoryMap
from acrobe.vfs import FsRoot


def memory_map(chunks, entry):
    m = MemoryMap(chunks)
    m.entry = entry
    return m


def test_encode_layout():
    data = Fx3BootImage.encode(memory_map(
        [(0x40000000, bytes([1, 0, 0, 0, 2, 0, 0, 0])), (0x100, b"\x03")],
        0x40000000), 0x20)
    assert data[:4] == b"CY\x20\xb0"
    words = struct.unpack(f"<{(len(data) - 4) // 4}L", data[4:])
    assert words == (1, 0x100, 3, 2, 0x40000000, 1, 2, 0, 0x40000000, 6)


def test_encode_splits_sections():
    data = Fx3BootImage.encode(memory_map(
        [(0x40000000, bytes(Fx3BootImage.SECTION_MAX + 4))], 0x40000000), 0)
    first, = struct.unpack_from("<L", data, 4)
    assert first == Fx3BootImage.SECTION_MAX // 4
    second = struct.unpack_from("<2L", data, 4 + 8 + Fx3BootImage.SECTION_MAX)
    assert second == (1, 0x40000000 + Fx3BootImage.SECTION_MAX)


@pytest.mark.parametrize("m,ctl", [
    (memory_map([(0x100, b"\0" * 4)], None), 0),
    (memory_map([(0x101, b"\0" * 4)], 0x100), 0),
    (memory_map([(0x100, b"\0" * 4)], 0x100), 1),
])
def test_encode_rejects(m, ctl):
    with pytest.raises(ValueError):
        Fx3BootImage.encode(m, ctl)


async def open_image(tmp_path, data):
    (tmp_path / "fw.img").write_bytes(data)
    root = FsRoot(str(tmp_path))
    await root.start_tree()
    node = await root.child_summon("fw.img")
    await node.start_tree()
    return node


async def test_parse_round_trip(tmp_path):
    original = memory_map([(0x40000000, bytes(range(16))),
                           (0x40003000, b"tail")], 0x40000004)
    node = await open_image(tmp_path, Fx3BootImage.encode(original, 0x1c))
    m = await MemoryMap.from_node(node)
    assert m.entry == 0x40000004
    assert m.simplified().chunks == original.simplified().chunks
    assert node.metadata["image_ctl"] == 0x1c


async def test_parse_bad_checksum(tmp_path):
    data = bytearray(Fx3BootImage.encode(
        memory_map([(0x40000000, bytes(4))], 0x40000000), 0))
    data[-1] ^= 1
    with pytest.raises(ValueError):
        await open_image(tmp_path, bytes(data))
