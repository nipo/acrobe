"""tar archive (and tar.gz) as a VFS Node container.

Per design D2: the tar root is a pure structural Node — entries
read through the parent's Readable.
"""

import io
import tarfile

from ..node import Node, Readable
from ..db import NoMatch
from . import FormatNode, register_format, register_magic
from ._tree import build_archive_tree


class TarEntry(Node, Readable):
    """A single file entry inside a tar archive.

    Reads extract the entry on first access and cache the result.
    """

    def __init__(self, name, archive_source, info):
        super().__init__(name)
        self.__archive_source = archive_source
        self.__info = info
        self.__cached = None

    @property
    def size(self) -> int:
        return self.__info.size

    async def start(self):
        from . import auto_populate
        await auto_populate(self, self, self.name)

    async def __ensure_data(self):
        if self.__cached is not None:
            return
        all_bytes = await self.__archive_source.read(
            0, self.__archive_source.size)
        with tarfile.open(fileobj=io.BytesIO(all_bytes)) as tf:
            f = tf.extractfile(self.__info)
            if f is None:
                # Symlink/special — not file data
                raise ValueError(
                    f"{self.fqdn}: tar entry has no extractable data")
            self.__cached = f.read()

    async def read(self, offset, size):
        if offset < 0 or offset > self.__info.size:
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
            "size": self.__info.size,
            "mtime": self.__info.mtime,
            "mode": self.__info.mode,
            "type": self.__info.type.decode("ascii", errors="replace")
            if isinstance(self.__info.type, bytes) else self.__info.type,
            **self._metadata,
        }


@register_format("tar",
                 exts=["tar", "tar.gz", "tgz", "tar.bz2", "tar.xz"],
                 mimes=["application/x-tar"])
class TarArchive(FormatNode):
    async def start(self):
        raw = await self._source.read(0, self._source.size)
        try:
            tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
        except tarfile.TarError as exc:
            raise NoMatch("tar", str(exc))
        try:
            entries = []
            for info in tf.getmembers():
                if not info.isfile():
                    continue
                entries.append((
                    info.name,
                    lambda name, info=info: TarEntry(
                        name, self._source, info),
                ))
            build_archive_tree(self, entries)
            self.metadata["entry_count"] = len(entries)
        finally:
            tf.close()


@register_magic
def _tar_magic(head: bytes):
    # Tar magic appears at offset 257 of the first 512-byte block,
    # not at the start. We need at least 263 bytes.
    if len(head) >= 263 and head[257:262] in (b"ustar", b"ustar"):
        return "tar"
    if len(head) >= 263 and head[257:265] == b"ustar  \x00":
        return "tar"
    # Note: tar.gz / tar.bz2 / tar.xz magic is for the wrapper. We
    # don't try to detect those generically — the .tar.gz / .tgz
    # extensions handle them via tarfile's "r:*" auto-mode.
    return None
