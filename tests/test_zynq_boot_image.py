import struct

import pytest

import acrobe.component.xilinx.boot_image  # noqa: F401
from acrobe.component.xilinx.boot_image import Destination
from acrobe.vfs import FsRoot


def checksummed(words):
    return list(words) + [~sum(words) & 0xffffffff]


def packed_name(name: bytes) -> bytes:
    name += b"\0" * (4 - len(name) % 4)
    return b"".join(name[i:i + 4][::-1] for i in range(0, len(name), 4))


def boot_image(*, key_source=0, register_init=(), rsa=False):
    """FSBL at 0x1000, a PS partition at 0x1100 and a PL partition at
    0x1200, image headers at 0x8c0, partition headers at 0xc80."""
    fsbl = bytes(range(16))
    app = b"APPLICATION!"
    pl = struct.pack("<2L", 0xaa995566, 0x20000000)
    raw = bytearray(0x1300)
    struct.pack_into("<8L", raw, 0, *[0xeafffffe] * 8)
    struct.pack_into("<11L", raw, 0x20, *checksummed([
        0xaa995566, 0x584c4e58, key_source, 0x01010000,
        0x1000, len(fsbl), 0, 0, len(fsbl), 1]))
    struct.pack_into("<21L", raw, 0x4c, *([0] * 19 + [0x8c0, 0xc80]))
    inits = list(register_init) + [(0xffffffff, 0)] * (256 - len(register_init))
    for i, (address, value) in enumerate(inits):
        struct.pack_into("<2L", raw, 0xa0 + 8 * i, address, value)
    struct.pack_into("<5L", raw, 0x8c0, 0x01020000, 3, 0xc80 // 4, 0x900 // 4, 0)
    images = [(0x900, b"fsbl.elf", 0xc80), (0x940, b"app.bin", 0xcc0),
              (0x980, b"top.bit", 0xd00)]
    for i, (offset, name, header) in enumerate(images):
        next_word = images[i + 1][0] // 4 if i + 1 < len(images) else 0
        struct.pack_into("<4L", raw, offset, next_word, header // 4, 0, 1)
        raw[offset + 16:offset + 16 + len(packed_name(name))] = packed_name(name)
    parts = [(fsbl, 0x1000, 0, 0, 0x10, 0x900),
             (app, 0x1100, 0x4000000, 0x4000000, 0x10 | (rsa << 15), 0x940),
             (pl, 0x1200, 0, 0, 0x20, 0x980)]
    for i, (data, offset, load, entry, attributes, image) in enumerate(parts):
        words = len(data) // 4
        struct.pack_into("<16L", raw, 0xc80 + 64 * i, *checksummed([
            words, words, words, load, entry, offset // 4, attributes, 1,
            0, image // 4, 0, 0, 0, 0, 0]))
        raw[offset:offset + len(data)] = data
    struct.pack_into("<16L", raw, 0xc80 + 64 * len(parts), *checksummed([0] * 15))
    return bytes(raw)


async def open_image(tmp_path, data):
    (tmp_path / "boot.bin").write_bytes(data)
    root = FsRoot(str(tmp_path))
    await root.start_tree()
    image = await root.child_summon("boot.bin")
    await image.start_tree()
    return image


async def test_partitions(tmp_path):
    image = await open_image(tmp_path, boot_image(register_init=[(0xf8000100, 5)]))
    assert image.metadata["register_init"] == [(0xf8000100, 5)]
    fsbl = image.child_lookup("fsbl")
    assert await fsbl.read(0, fsbl.size) == bytes(range(16))
    partitions = image.child_lookup("partition").children
    assert [p.metadata["name"] for p in partitions] == \
        ["fsbl.elf", "app.bin", "top.bit"]
    app = partitions[1]
    assert app.metadata["destination"] == Destination.PS
    assert app.metadata["load_address"] == 0x4000000
    assert await app.read(0, app.size) == b"APPLICATION!"
    bitstream = partitions[2].child_lookup("bitstream")
    assert partitions[2].metadata["destination"] == Destination.PL
    assert await bitstream.read(0, 8) == bytes.fromhex("aa99556620000000")


async def test_encrypted_rejected(tmp_path):
    with pytest.raises(NotImplementedError):
        await open_image(tmp_path, boot_image(key_source=0xa5c3c5a3))


async def test_authenticated_rejected(tmp_path):
    with pytest.raises(NotImplementedError):
        await open_image(tmp_path, boot_image(rsa=True))


async def test_bad_checksum(tmp_path):
    data = bytearray(boot_image())
    data[0x30] ^= 1
    with pytest.raises(ValueError):
        await open_image(tmp_path, bytes(data))
