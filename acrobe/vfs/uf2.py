"""UF2 (.uf2) — USB Flashing Format used by RP2040 / RP2350 and a
handful of other Cortex-M targets.

A UF2 file is a sequence of 512-byte blocks. Each block carries up
to 256 payload bytes plus a header recording the target address,
optional family ID, and various flags. The format is structured so
the receiving USB MSC can copy the file block-by-block and drop
unrecognised content (e.g. metadata blocks for other families).

Block layout (Microsoft / Adafruit spec):

    +0x00  magicStart0       0x0A324655   ("UF2\\n")
    +0x04  magicStart1       0x9E5D5157
    +0x08  flags             u32 bitfield
    +0x0C  targetAddr        u32  destination address
    +0x10  payloadSize       u32  ≤ 256
    +0x14  blockNo           u32  0..numBlocks-1
    +0x18  numBlocks         u32
    +0x1C  fileSize          u32  -- or familyID when flag bit 13 set
    +0x20  data              476 bytes (first payloadSize valid)
    +0x1FC magicEnd          0x0AB16F30

Flags consulted here:
    bit  0 (NOT_MAIN_FLASH)  — skip block (metadata / config).
    bit 13 (FAMILY_ID_PRESENT) — fileSize field carries a family ID.

Family IDs surface in metadata so downstream code can pick a path
(e.g. RP2040 vs RP2350) when the file is multi-family. Filtering
on family ID is the caller's job; the parser emits every payload
block it finds, in the order the file presents them.

Output shape (mirrors `ihex.py`):

- ``region/N`` Node children — one `Uf2Region` per contiguous
  run of consecutive `targetAddr`s. Each region is
  `Readable + Addressable`, so the regular loader walks them as
  any other addressable leaf.
- Metadata: ``block_count``, ``min_address``, ``max_address``,
  ``family_ids`` (sorted list of distinct family IDs seen).
"""

from __future__ import annotations

import struct

from ..db import NoMatch
from ..node import Node, Readable, Addressable
from . import FormatNode, register_format


UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END    = 0x0AB16F30

UF2_FLAG_NOT_MAIN_FLASH    = 1 << 0
UF2_FLAG_FAMILY_ID_PRESENT = 1 << 13

UF2_BLOCK_SIZE = 512
UF2_MAX_PAYLOAD = 256

# Common family IDs surfaced in metadata. Not consulted for routing
# here — the loader picks a target before the parser runs.
UF2_FAMILY_RP2040       = 0xE48BFF56
UF2_FAMILY_RP2350_ABS   = 0xE48BFF57
UF2_FAMILY_RP2350_RISCV = 0xE48BFF58
UF2_FAMILY_RP2350_ARM_S = 0xE48BFF59


class Uf2Region(Node, Readable, Addressable):
    """One contiguous run of payload bytes from a UF2 file."""

    def __init__(self, name, address, data: bytes):
        super().__init__(name)
        self._address = address
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self._data):
            raise ValueError(f"offset {offset} out of range")
        n = min(size, len(self._data) - offset)
        if n <= 0:
            return b""
        return self._data[offset:offset + n]

    @property
    def load_address(self) -> int:
        return self._address


@register_format("uf2",
                 exts=["uf2"],
                 mimes=["application/x-uf2"])
class Uf2(FormatNode):
    """UF2 file parser. See module docstring for the block layout
    and output shape."""

    async def start(self):
        raw = await self._source.read(0, self._source.size)
        if len(raw) % UF2_BLOCK_SIZE != 0:
            raise NoMatch(
                "uf2",
                f"file size {len(raw)} not a multiple of "
                f"{UF2_BLOCK_SIZE}")
        if len(raw) == 0:
            raise NoMatch("uf2", "empty file")

        regions, family_ids = self._parse(raw)
        if not regions:
            raise NoMatch("uf2", "no payload blocks")

        region_container = Node("region")
        for i, (addr, data) in enumerate(regions):
            region_container._child_attach(Uf2Region(str(i), addr, data))
        self._child_attach(region_container)

        self._metadata["block_count"] = len(raw) // UF2_BLOCK_SIZE
        self._metadata["min_address"] = min(a for a, _ in regions)
        self._metadata["max_address"] = max(
            a + len(d) for a, d in regions)
        self._metadata["family_ids"] = sorted(family_ids)

    @staticmethod
    def _parse(raw: bytes):
        regions: list[tuple[int, bytearray]] = []
        family_ids: set[int] = set()
        cur_addr: int | None = None
        cur_data: bytearray | None = None

        for block_no, off in enumerate(range(0, len(raw), UF2_BLOCK_SIZE)):
            block = raw[off:off + UF2_BLOCK_SIZE]
            (magic0, magic1, flags, target_addr,
             payload_size, _block_idx, _num_blocks,
             file_or_family) = struct.unpack("<IIIIIIII", block[:32])
            magic_end = struct.unpack("<I", block[-4:])[0]

            if magic0 != UF2_MAGIC_START0 or magic1 != UF2_MAGIC_START1:
                raise NoMatch(
                    "uf2",
                    f"block {block_no}: magic start mismatch "
                    f"(got 0x{magic0:08x} 0x{magic1:08x})")
            if magic_end != UF2_MAGIC_END:
                raise NoMatch(
                    "uf2",
                    f"block {block_no}: magic end mismatch "
                    f"(got 0x{magic_end:08x})")

            if flags & UF2_FLAG_NOT_MAIN_FLASH:
                continue
            if flags & UF2_FLAG_FAMILY_ID_PRESENT:
                family_ids.add(file_or_family)

            if payload_size > UF2_MAX_PAYLOAD:
                raise NoMatch(
                    "uf2",
                    f"block {block_no}: payload_size {payload_size} "
                    f"> {UF2_MAX_PAYLOAD}")
            payload = bytes(block[32:32 + payload_size])

            if (cur_addr is not None
                    and target_addr == cur_addr + len(cur_data)):
                cur_data.extend(payload)
            else:
                if cur_data:
                    regions.append((cur_addr, bytes(cur_data)))
                cur_addr = target_addr
                cur_data = bytearray(payload)

        if cur_data:
            regions.append((cur_addr, bytes(cur_data)))
        return regions, family_ids
