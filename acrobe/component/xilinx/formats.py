"""Xilinx .bit bitstream format as a VFS Node.

Migrated from `acrobe.loadable.xilinx`. Path: `file.bit/bitstream`
gives the raw payload bytes (no bit-swap). Header metadata (device,
project, build date, userid) is exposed via `metadata`.
"""

import gzip
import struct

from ...db import NoMatch
from ...node import Node, Readable
from ...vfs import FormatNode, register_format, register_magic


_HEADER = bytes([
    0x00, 0x09, 0x0f, 0xf0, 0x0f, 0xf0,
    0x0f, 0xf0, 0x0f, 0xf0, 0x00, 0x00, 0x01,
])


class _BytesReadable(Readable):
    """In-memory Readable backing (used after gzip decompress)."""

    def __init__(self, data: bytes):
        self.__data = data

    @property
    def size(self) -> int:
        return len(self.__data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self.__data):
            raise ValueError(f"offset {offset} out of range")
        avail = len(self.__data) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return self.__data[offset:offset + n]


class XilinxPayload(Node, Readable):
    """Raw bitstream payload bytes from a .bit file."""

    def __init__(self, name, source, offset, size):
        super().__init__(name)
        self.__source = source
        self.__offset = offset
        self.__size = size

    @property
    def size(self) -> int:
        return self.__size

    async def read(self, offset, size):
        if offset < 0 or offset > self.__size:
            raise ValueError(f"offset {offset} out of range")
        avail = self.__size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return await self.__source.read(self.__offset + offset, n)


@register_format("xilinx_bit",
                 exts=["bit", "bit.gz"],
                 mimes=["application/x-xilinx-bit"])
class XilinxBit(FormatNode):
    """Parses a Xilinx .bit file (optionally gzip-wrapped).

    Lettered sections (a/b/c/d) hold metadata; section 'e' holds the
    bitstream payload. Children:
    - bitstream: XilinxPayload — the raw payload.
    """

    async def start(self):
        raw = await self._source.read(0, self._source.size)
        # Auto-detect gzip wrapper.
        if raw[:2] == b"\x1f\x8b":
            blob = gzip.decompress(raw)
            backing = _BytesReadable(blob)
            self.metadata["gzipped"] = True
            backing_offset = 0
        else:
            blob = raw
            backing = self._source
            backing_offset = 0

        if blob[:len(_HEADER)] != _HEADER:
            raise NoMatch("xilinx_bit", "header")

        info = {}
        idx = len(_HEADER)
        payload_offset = None
        payload_size = None

        while idx < len(blob):
            section = blob[idx:idx + 1]
            idx += 1
            if not section:
                break

            if section == b"e":
                if idx + 4 > len(blob):
                    raise ValueError(f"{self.fqdn}: short 'e' header")
                size = struct.unpack(">L", blob[idx:idx + 4])[0]
                idx += 4
                if idx + size > len(blob):
                    raise ValueError(
                        f"{self.fqdn}: short payload "
                        f"(have {len(blob) - idx}, need {size})")
                payload_offset = backing_offset + idx
                payload_size = size
                idx += size
                break

            if idx + 2 > len(blob):
                raise ValueError(
                    f"{self.fqdn}: short header for section {section!r}")
            sz = struct.unpack(">H", blob[idx:idx + 2])[0]
            idx += 2
            if idx + sz > len(blob):
                raise ValueError(
                    f"{self.fqdn}: short payload for section {section!r}")
            data = blob[idx:idx + sz]
            idx += sz
            info[section] = str(data.rstrip(b"\x00"), "utf-8", "ignore")

        if payload_offset is None:
            raise ValueError(f"{self.fqdn}: no bitstream payload section")

        # Promote info into metadata
        if b"a" in info:
            parts = info[b"a"].split(";")
            self.metadata["project"] = parts[0]
            for p in parts[1:]:
                if "=" not in p:
                    continue
                k, v = p.split("=", 1)
                k = k.lower().strip()
                v = v.strip()
                if k == "userid":
                    try:
                        v = int(v, 16)
                    except ValueError:
                        pass
                self.metadata[k] = v
        if b"b" in info:
            self.metadata["device"] = info[b"b"]
        if b"c" in info and b"d" in info:
            self.metadata["build_date"] = (
                info[b"c"].strip() + " " + info[b"d"].strip())

        view = XilinxPayload("bitstream", backing, payload_offset, payload_size)
        self.child_add(view)


@register_magic
def _xilinx_bit_magic(head: bytes):
    if head[:len(_HEADER)] == _HEADER:
        return "xilinx_bit"
    return None
