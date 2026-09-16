"""Physical memory windows through ``/dev/mem``.

Path syntax::

    devmem/<base>:<size>[/<client>]

``devmem`` resolves to a :class:`DevMemBroker`; the next component
names a physical window (both numbers in any ``int(x, 0)`` form) and
resolves to a :class:`DevMemWindow`, a `memory.Interface` node whose
address space is the window itself: address 0 is ``base`` and any op
touching a byte outside ``[0, size)`` is rejected rather than clipped.
Clients registered on `memory.Interface.child_db` hang off the
window, so a memory-mapped peripheral is
``devmem/0x40000000:0x1000/<driver>``.

Register ops (``read8/16/32``, ``write8/16/32``) are issued as one
naturally-sized, naturally-aligned access each, which is what a
register-mapped peripheral needs to see. Bulk ops (``mem_read``,
``mem_write``) are plain memory copies through the mapping.

``/dev/mem`` needs root or ``CAP_SYS_RAWIO``. With
``CONFIG_STRICT_DEVMEM`` the kernel refuses to map RAM ranges
(``EPERM`` at ``mmap``), and touching a physical range with nothing
behind it raises ``SIGBUS`` in the process. Neither is caught here.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import re

from ...db import NoMatch
from ...engine import Batcher
from ...lifecycle import cancel_shutdown, on_shutdown
from ...node import Node
from ...protocol import memory
from ..model import Enumerator, enumerator_db
from .ioctl import Ioctl  # noqa: F401 — carries the platform guard


_WINDOW_RE = re.compile(
    r"^(?P<base>[0-9A-Za-z_]+):(?P<size>[0-9A-Za-z_]+)$")


def _parse_window(name: str) -> tuple[int, int]:
    m = _WINDOW_RE.match(name)
    if not m:
        raise ValueError(f"not a base:size window: {name!r}")
    base = int(m.group("base"), 0)
    size = int(m.group("size"), 0)
    if size <= 0:
        raise ValueError(f"window size must be positive: {name!r}")
    return base, size


class DevMemWindow(memory.Interface, Batcher, Node):
    """One ``[base, base+size)`` physical range, mapped for the node's
    lifetime and addressed relative to ``base``.

    The mapping is page-granular, so it may cover bytes before
    ``base`` and after ``base+size``; bounds are checked in window
    coordinates before translation so those bytes are never
    reachable.
    """

    ops = memory.Interface.REGISTER_OPS | memory.Interface.BULK_OPS

    # Register op -> (ctypes accessor, access width in bytes).
    REGISTER_ACCESS = {
        memory.Read8: (ctypes.c_uint8, 1),
        memory.Read16: (ctypes.c_uint16, 2),
        memory.Read32: (ctypes.c_uint32, 4),
        memory.Write8: (ctypes.c_uint8, 1),
        memory.Write16: (ctypes.c_uint16, 2),
        memory.Write32: (ctypes.c_uint32, 4),
    }

    def __init__(self, base: int, size: int, *,
                 path: str = "/dev/mem", name: str | None = None):
        Batcher.__init__(self)
        Node.__init__(self, name or f"{base:#x}:{size:#x}")
        if base < 0 or size <= 0:
            raise ValueError(
                f"invalid window base={base:#x} size={size:#x}")
        self.base = base
        self.size = size
        self.device_path = path
        self.__mapping: mmap.mmap | None = None
        self.__delta = 0
        self.metadata.update(base=base, size=size, path=path)

    async def start(self) -> None:
        if self.__mapping is not None:
            return
        gran = mmap.ALLOCATIONGRANULARITY
        page_base = self.base & ~(gran - 1)
        delta = self.base - page_base
        length = (delta + self.size + gran - 1) & ~(gran - 1)
        fd = os.open(self.device_path, os.O_RDWR | os.O_SYNC | os.O_CLOEXEC)
        try:
            mapping = mmap.mmap(fd, length, flags=mmap.MAP_SHARED,
                                prot=mmap.PROT_READ | mmap.PROT_WRITE,
                                offset=page_base)
        finally:
            os.close(fd)
        self.__mapping = mapping
        self.__delta = delta
        self.metadata.update(page_base=page_base, length=length)
        self.logger.info("mapped %s [%#x, %#x)", self.device_path,
                         self.base, self.base + self.size)
        on_shutdown(self.stop)

    async def stop(self) -> None:
        cancel_shutdown(self.stop)
        mapping, self.__mapping = self.__mapping, None
        if mapping is not None:
            # A BufferError here means a ctypes accessor outlived its
            # op, which __run never allows.
            mapping.close()

    async def flush_ops(self, batch):
        for op, future in batch:
            try:
                result = self.__run(op)
            except Exception as exc:
                if future is not None:
                    future.set_exception(exc)
                continue
            if future is not None:
                future.set_result(result)

    def __offset(self, addr: int, width: int, aligned: bool) -> int:
        """Validate a window-relative access and return its offset in
        the mapping."""
        if self.__mapping is None:
            raise RuntimeError(f"{self.fqdn}: window is not mapped")
        if addr < 0 or addr + width > self.size:
            raise ValueError(
                f"{self.fqdn}: access {addr:#x}+{width} outside "
                f"window [0, {self.size:#x})")
        if aligned and addr % width:
            raise ValueError(
                f"{self.fqdn}: {width}-byte access at {addr:#x} "
                f"is unaligned")
        return self.__delta + addr

    def __run(self, op):
        access = self.REGISTER_ACCESS.get(type(op))
        if access is not None:
            ctype, width = access
            off = self.__offset(op.addr, width, aligned=True)
            if isinstance(op, (memory.Read8, memory.Read16,
                               memory.Read32)):
                return ctype.from_buffer(self.__mapping, off).value
            if op.data < 0 or op.data >> (8 * width):
                raise ValueError(
                    f"{self.fqdn}: value {op.data:#x} does not fit "
                    f"a {width}-byte register")
            ctype.from_buffer(self.__mapping, off).value = op.data
            return None
        if isinstance(op, memory.ReadBlob):
            off = self.__offset(op.addr, op.size, aligned=False)
            return bytes(self.__mapping[off:off + op.size])
        if isinstance(op, memory.WriteBlob):
            off = self.__offset(op.addr, len(op.data), aligned=False)
            self.__mapping[off:off + len(op.data)] = op.data
            return None
        raise TypeError(
            f"{type(self).__name__} can't lower {type(op).__name__}")


class DevMemBroker(Node):
    """The ``devmem`` directory under :class:`HwRoot`.

    Resolves ``devmem/<base>:<size>`` to a fresh :class:`DevMemWindow`.
    Owns no state of its own; windows are user-chosen, so there is no
    hint list.
    """

    def __init__(self, name: str = "devmem", *, path: str = "/dev/mem"):
        super().__init__(name)
        self.__path = path
        self.metadata["path"] = path

    async def child_spawn(self, name: str) -> DevMemWindow:
        try:
            base, size = _parse_window(name)
        except ValueError:
            raise NoMatch("devmem-window", name)
        return DevMemWindow(base, size, path=self.__path, name=name)


@enumerator_db.register("devmem")
class DevMemEnumerator(Enumerator):
    """Attaches the single :class:`DevMemBroker` namespace node.

    Attached whether or not ``/dev/mem`` is usable: an unreadable
    device surfaces as the real ``OSError`` when a window starts.
    """

    def __init__(self, path: str = "/dev/mem"):
        self.__path = path

    async def populate(self, hw_root):
        if not hw_root.has_child("devmem"):
            hw_root.child_add(DevMemBroker(path=self.__path))
