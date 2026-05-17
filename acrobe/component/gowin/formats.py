"""Gowin .fs / .fs.gz bitstream format as a VFS Node.

Migrated from `acrobe.loadable.gowin`. The .fs format is a text
file: a header of `// key: value` comments, followed by lines of
'0'/'1' bits. The bitstream is packed MSB-first into bytes,
then byte-reversed (matches the Gowin loader convention).

Children:
- bitstream: GowinPayload — packed binary bytes.

Metadata: header keys (Device, UserCode, Checksum, etc.)
"""

import gzip
from collections import deque

from ...db import NoMatch
from ...node import Node, Readable
from ...bitstring import BitString
from ...vfs import FormatNode, register_format, register_magic


class GowinPayload(Node, Readable):
    """In-memory bitstream payload from a .fs file.

    Held as bytes since the .fs format is text and parsing is
    cheaper if done eagerly into a buffer.
    """

    def __init__(self, name, data: bytes):
        super().__init__(name)
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


@register_format("gowin_fs",
                 exts=["fs", "fs.gz"],
                 mimes=["application/x-gowin-fs"])
class GowinFs(FormatNode):
    async def start(self):
        raw = await self._source.read(0, self._source.size)

        # Detect gzip wrapper
        if raw[:2] == b"\x1f\x8b":
            text = gzip.decompress(raw).decode("utf-8", errors="ignore")
            self.metadata["gzipped"] = True
        else:
            text = raw.decode("utf-8", errors="ignore")

        lines = deque(text.splitlines(keepends=True))
        info = {}
        while lines and lines[0].startswith("//"):
            line = lines.popleft().strip()[2:]
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()

        stream = "".join(line.strip() for line in lines)
        if not stream:
            raise NoMatch("gowin_fs", "no bitstream data")
        for ch in stream:
            if ch not in "01":
                raise NoMatch("gowin_fs", "non-binary chars in body")

        # Pack bits, MSB-first, then byte-reverse (matches loader).
        data = BitString(int(stream, 2), len(stream))
        raw_bytes = bytes(data)[::-1]
        self.metadata.update(info)

        view = GowinPayload("bitstream", raw_bytes)
        self.child_add(view)


# Gowin .fs files are text-based; their "magic" is the leading "//"
# comment header. That's not specific enough to register as a magic
# detector — leave to the .fs / .fs.gz extension.
