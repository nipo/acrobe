"""ZIP archive as a VFS Node container.

Per design D2: the ZIP root is a pure structural Node (no Readable
of its own — the parent file Node already covers raw ZIP bytes).
Entries are laid out as a tree by splitting their archive paths on
"/" (handled by `_tree.build_archive_tree`).

ZIP entries hold a reference to the parent's Readable; reads
decompress on demand into memory once, then slice. This trades
warm-cache latency for memory but matches typical use (entries
fully consumed, not random-accessed).
"""

import zipfile
import io

from ..node import Node, Readable
from ..db import NoMatch
from . import FormatNode, register_format, register_magic
from ._tree import build_archive_tree


class ZipEntry(Node, Readable):
    """A single file entry inside a ZIP archive.

    Reads decompress the entry on first access and cache the result.
    """

    def __init__(self, name, archive_source, info):
        super().__init__(name)
        self.__archive_source = archive_source
        self.__info = info
        self.__cached = None

    @property
    def size(self) -> int:
        return self.__info.file_size

    async def start(self):
        # Auto-detect format on this entry's content (e.g. for
        # archive.zip/inner.pof where inner.pof is itself a POF
        # to be parsed structurally).
        from . import auto_populate
        await auto_populate(self, self, self._name)

    async def __ensure_data(self):
        if self.__cached is not None:
            return
        # Read entire archive bytes (zipfile needs a file-like).
        # For most use cases, the archive's parent is already a
        # FileNode; reading once into memory is acceptable.
        all_bytes = await self.__archive_source.read(
            0, self.__archive_source.size)
        with zipfile.ZipFile(io.BytesIO(all_bytes)) as zf:
            self.__cached = zf.read(self.__info.filename)

    async def read(self, offset, size):
        if offset < 0 or offset > self.__info.file_size:
            raise ValueError(f"offset {offset} out of range")
        await self.__ensure_data()
        avail = len(self.__cached) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return self.__cached[offset:offset + n]

    @property
    def metadata(self) -> dict:
        return {
            "compress_type": self.__info.compress_type,
            "compress_size": self.__info.compress_size,
            "uncompressed_size": self.__info.file_size,
            "crc": self.__info.CRC,
            "date_time": self.__info.date_time,
            **self._metadata,
        }


@register_format("zip",
                 exts=["zip"],
                 mimes=["application/zip"])
class ZipArchive(FormatNode):
    """ZIP container. Children mirror the archive directory tree."""

    async def start(self):
        # zipfile needs a file-like; read whole archive.
        # Most ZIPs are small enough that this is fine.
        raw = await self._source.read(0, self._source.size)
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise NoMatch("zip", str(exc))
        try:
            entries = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Capture info via default arg to avoid loop closure issue.
                entries.append((
                    info.filename,
                    lambda name, info=info: ZipEntry(
                        name, self._source, info),
                ))
            build_archive_tree(self, entries)
            self._metadata["entry_count"] = len(entries)
        finally:
            zf.close()


@register_magic
def _zip_magic(head: bytes):
    # Local file header signature: 'PK\x03\x04'.
    if head[:4] == b"PK\x03\x04":
        return "zip"
    # Empty archive end-of-central-directory at offset 0.
    if head[:4] == b"PK\x05\x06":
        return "zip"
    return None
