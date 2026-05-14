"""SOF (Quartus Output File) format parser.

Also hosts the TLV section parser and `SofSection` enum reused by POF.
"""

import enum
import gzip
import struct

from ....db import NoMatch
from ....node import Node, Readable
from ....vfs import (
    FormatNode,
    register_format,
    register_magic,
)


SOF_MAGIC = b"SOF\x00"


class SofSection(enum.IntEnum):
    TOOL = 0x01
    DEVICE = 0x02  # SOF: target device; POF: flash device
    DESIGN = 0x03
    END = 0x08
    CONFIG_DATA = 0x11
    CONFIG_INFO = 0x12
    METADATA = 0x13
    CHECKSUM = 0x15
    BOOT_INFO = 0x1a
    EMBEDDED = 0x24
    BOOTLOADER_DATA = 0x3e  # HPS bootloader payload (Agilex+HPS designs)


def _parse_sections(blob: bytes):
    """Iterate over POF/SOF tag-length-value sections.

    Yields (tag, flags, data). Stops on END tag or when blob ends."""
    idx = 12  # skip magic(4) + version(4) + count(4)
    while idx + 6 <= len(blob):
        tag = blob[idx]
        flags = blob[idx + 1]
        length = struct.unpack_from("<I", blob, idx + 2)[0]
        data = blob[idx + 6:idx + 6 + length]
        idx += 6 + length
        yield tag, flags, data
        if tag == SofSection.END:
            break


def _decode_string(data: bytes) -> str:
    return data.rstrip(b"\x00").decode("ascii", errors="replace")


def _decode_bootloader_data(section: bytes) -> bytes:
    """Decode a SOF Bootloader Data section (tag 0x3e) to its original
    payload bytes (typically the contents of an ihex file passed to
    Quartus' `quartus_pfg -c X.sof Y.sof -o hps_path=Z.hex`).

    Empirically reverse-engineered against quartus_pfg output; matches
    the original ihex byte-for-byte.

    Section layout
    --------------
    - 16-byte file-level header (opaque metadata).
    - One or more blocks holding the payload. Each pair of consecutive
      bytes encodes one payload byte: one of the two bytes is the
      constant marker 0x11, the other is the data byte. Which slot
      holds the data alternates per block; the parity is detected from
      the first two words of the block.
    - Block 0's first decoded byte is a 0x00 sentinel (stripped here).
      To accommodate the sentinel, block 0 carries one extra payload
      byte as a lone byte (no marker) right before the first
      sub-header.
    - 11-byte sub-header between blocks, signature
      ``34 12 00 00 NN 00 12`` followed by a u32-LE giving the next
      block's decoded-byte size. NN is a 1-based block index.
    - Last block runs to the end of the section.
    """
    if len(section) < 16:
        raise ValueError("bootloader-data section too short for 16-byte header")
    body = section[16:]

    sub_positions = []
    i = 0
    while i + 11 <= len(body):
        if (body[i:i + 4] == b"\x34\x12\x00\x00"
                and body[i + 5] == 0x00
                and body[i + 6] == 0x12):
            sub_positions.append(i)
            i += 11
        else:
            i += 1

    def detect_parity(off: int) -> int:
        if off + 4 > len(body):
            raise ValueError(f"can't detect parity at body[{off:#x}]: short")
        a0, b0 = body[off], body[off + 1]
        a1, b1 = body[off + 2], body[off + 3]
        if b0 == 0x11 and b1 == 0x11:
            return 1
        if a0 == 0x11 and a1 == 0x11:
            return 0
        raise ValueError(f"can't detect parity at body[{off:#x}]")

    out = bytearray()

    def decode_words(start: int, end: int, parity: int) -> None:
        for j in range(start, end - 1, 2):
            a, b = body[j], body[j + 1]
            if parity == 1 and b == 0x11:
                out.append(a)
            elif parity == 0 and a == 0x11:
                out.append(b)
            else:
                raise ValueError(
                    f"bootloader data: bad pair at body[{j:#x}]: "
                    f"{a:#04x} {b:#04x} (parity {parity})")

    cursor = 0
    for idx, sub_pos in enumerate(sub_positions):
        parity = detect_parity(cursor)
        if idx == 0:
            decode_words(cursor, sub_pos - 1, parity)
            out.append(body[sub_pos - 1])  # lone byte (only after block 0)
        else:
            decode_words(cursor, sub_pos, parity)
        cursor = sub_pos + 11
    if cursor < len(body):
        parity = detect_parity(cursor)
        decode_words(cursor, len(body), parity)

    return bytes(out[1:])


class _BytesReadable(Readable):
    """In-memory Readable backing for decompressed buffers."""

    def __init__(self, data: bytes):
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self._data):
            raise ValueError(f"offset {offset} out of range")
        avail = len(self._data) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return self._data[offset:offset + n]


class SofConfigData(Node, Readable):
    """Raw CONFIG_DATA section of a SOF.

    Format-internal Quartus frame data — NOT RBF bitstream. Useful
    for inspection; converting to RBF requires Quartus internals.
    """

    def __init__(self, name, source, offset, size):
        super().__init__(name)
        self._source = source
        self._offset = offset
        self._size = size

    @property
    def size(self) -> int:
        return self._size

    async def read(self, offset, size):
        if offset < 0 or offset > self._size:
            raise ValueError(f"offset {offset} out of range")
        avail = self._size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return await self._source.read(self._offset + offset, n)


class SofBootloaderData(Node, Readable):
    """Decoded HPS bootloader payload from a SOF Bootloader Data section.

    Backed by an in-memory bytes buffer holding the framing-stripped
    payload. The bytes match the original ihex input that was inserted
    via ``quartus_pfg -c X.sof Y.sof -o hps_path=Z.hex``.
    """

    def __init__(self, name, data: bytes):
        super().__init__(name)
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


@register_format("altera_sof",
                 exts=["sof"],
                 mimes=["application/x-altera-sof"])
class Sof(FormatNode):
    """SOF (Quartus output) container.

    Auto-decompresses gzip-wrapped SOF (Agilex) before parsing.
    Exposes a `config_data` child holding raw CONFIG_DATA bytes.
    Metadata: tool, device, design, usercode.
    """

    def __init__(self, name, source):
        super().__init__(name, source)
        self.tool = None
        self.device = None
        self.design = None
        self.usercode = None

    async def start(self):
        raw = await self._source.read(0, self._source.size)
        if raw[:2] == b"\x1f\x8b":
            blob = gzip.decompress(raw)
            self._metadata["gzipped"] = True
            backing = _BytesReadable(blob)
        else:
            blob = raw
            backing = self._source

        if blob[:4] != SOF_MAGIC:
            raise NoMatch("altera_sof", "magic")

        idx = 12
        config_offset = None
        config_size = None
        bootloader_bytes = None
        for tag, flags, data in _parse_sections(blob):
            if tag == SofSection.CONFIG_DATA:
                config_offset = idx + 6
                config_size = len(data)
            elif tag == SofSection.TOOL:
                self.tool = _decode_string(data)
            elif tag == SofSection.DEVICE:
                self.device = _decode_string(data)
            elif tag == SofSection.DESIGN:
                self.design = _decode_string(data)
            elif tag == SofSection.METADATA and len(data) >= 16:
                self.usercode = struct.unpack_from("<I", data, 12)[0]
            elif tag == SofSection.BOOTLOADER_DATA:
                bootloader_bytes = _decode_bootloader_data(data)
            idx += 6 + len(data)

        self._metadata.update({
            "tool": self.tool,
            "device": self.device,
            "design": self.design,
            "usercode": self.usercode,
        })

        if config_offset is not None:
            self._child_attach(SofConfigData(
                "config_data", backing, config_offset, config_size))

        if bootloader_bytes is not None:
            self._metadata["bootloader_size"] = len(bootloader_bytes)
            self._child_attach(SofBootloaderData("bootloader", bootloader_bytes))


@register_magic
def _sof_magic(head: bytes):
    if head[:4] == SOF_MAGIC:
        return "altera_sof"
    if head[:2] == b"\x1f\x8b":
        # Could be a gzip-wrapped SOF, but also a generic gzip
        # archive. We can't decide without decompressing — leave
        # to other detectors / explicit `as(type=...)`.
        return None
    return None
