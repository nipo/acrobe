"""Owned file descriptor on a Linux character device."""

import fcntl
import os

from ...lifecycle import cancel_shutdown, on_shutdown
from .ioctl import Ioctl  # noqa: F401 — carries the platform guard


class CharDevice:
    """One ``/dev`` node, opened for the lifetime of its owner.

    Kept deliberately small and non-Node: it is a resource an
    interface owns, not a thing to show in the component tree.
    :meth:`call` is the single point where this package touches
    ``fcntl``, which is also what makes the device modules testable
    without hardware — a fake subclass overrides it and nothing else.

    ``call`` is synchronous. ioctls on these devices block for the
    duration of the bus transaction, so callers must run them off the
    event loop; they do it once per batch (a whole ``SPI_IOC_MESSAGE``,
    a whole I²C transaction sequence) rather than once per ioctl,
    which is why offloading is the caller's job and not done here.
    """

    def __init__(self, path: str):
        self.path = path
        self.__fd = None

    @property
    def fd(self) -> int:
        if self.__fd is None:
            raise RuntimeError(f"{self.path} is not open")
        return self.__fd

    @property
    def opened(self) -> bool:
        return self.__fd is not None

    async def open(self):
        if self.__fd is not None:
            return
        self.__fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC)
        on_shutdown(self.close)

    async def close(self):
        cancel_shutdown(self.close)
        fd, self.__fd = self.__fd, None
        if fd is not None:
            os.close(fd)

    def call(self, request: int, arg, mutate: bool = True) -> int:
        """Issue one ioctl. Returns the kernel's return value.

        `arg` is either an int (passed by value) or a ctypes object.
        A ctypes object is required whenever the kernel writes back or
        the argument exceeds 1024 bytes -- `fcntl.ioctl` copies a
        read-only buffer and hands back the copy instead of the
        return value, and refuses one that large outright.
        """
        return fcntl.ioctl(self.fd, request, arg, mutate)
