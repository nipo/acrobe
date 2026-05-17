"""File-system root Node and file-leaf Node.

`FsRoot` resolves children by file path on the local filesystem.
`FileNode` is the leaf type — opens the file lazily during start(),
exposes its bytes as a `Readable`.

These are the entry points for any VFS walk that begins on a real
file on disk. Once the leaf is open, format detection and
reinterpretation happen on top (see `acrobe.vfs` Step 5).
"""

import os

from ..db import NoMatch
from ..node import Node, Readable


class FileNode(Node, Readable):
    """A file leaf. Bytes are read from a file on disk.

    Lazily opens the file in start(); closes it in stop().
    Reads are async via run_in_executor — Python's `os.pread`
    is blocking, but we use it under the hood and run it in a
    thread to keep the Node IO contract async.
    """

    def __init__(self, name: str, path: str):
        super().__init__(name)
        self.__path = path
        self.__fd = None
        self.__size = 0

    @property
    def path(self) -> str:
        return self.__path

    @property
    def size(self) -> int:
        return self.__size

    async def start(self):
        # Open file and stat for size. os.open is blocking but
        # cheap; not worth offloading.
        self.__fd = os.open(self.__path, os.O_RDONLY)
        self.__size = os.fstat(self.__fd).st_size
        # Auto-detect format and populate children if recognised.
        from . import auto_populate
        await auto_populate(self, self, self._name)

    async def stop(self):
        if self.__fd is not None:
            os.close(self.__fd)
            self.__fd = None

    async def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or offset > self.__size:
            raise ValueError(
                f"offset {offset} out of range [0, {self.__size}]")
        if self.__fd is None:
            raise RuntimeError(f"{self.fqdn}: file not open (call start())")
        # os.pread is sync; run in default executor for async contract
        import asyncio
        loop = asyncio.get_running_loop()
        # Clamp size to remaining bytes (POSIX-pread semantics).
        avail = self.__size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return await loop.run_in_executor(
            None, os.pread, self.__fd, n, offset)

    @property
    def metadata(self) -> dict:
        # Merge our intrinsic file metadata with anything populated
        # by an auto-detected format.
        return {"path": self.__path, "size": self.__size, **self._metadata}


class FsRoot(Node):
    """Root Node anchoring children at filesystem paths.

    `child_summon("file.bin")` looks up "file.bin" relative to the
    root's directory (or absolute, if the name is absolute). The
    spawned child is a `FileNode` whose `start()` opens the file.

    Subdirectories are not auto-walked; if a user wants nested
    paths, they pass them as separate child_summon parts:
    `root.child_summon("subdir", "file.bin")` — though for typical
    VFS use, you'd just root the FsRoot at the directory you want.
    """

    def __init__(self, base_dir: str = "."):
        super().__init__(name=os.path.abspath(base_dir))
        self.__base_dir = os.path.abspath(base_dir)

    @property
    def base_dir(self) -> str:
        return self.__base_dir

    async def child_spawn(self, name: str) -> Node:
        # Resolve the path. Allow absolute names; otherwise
        # relative to base_dir.
        if os.path.isabs(name):
            path = name
        else:
            path = os.path.join(self.__base_dir, name)
        if os.path.isdir(path):
            return FsRoot(path)
        if os.path.isfile(path):
            return FileNode(name, path)
        raise NoMatch("file", name)
