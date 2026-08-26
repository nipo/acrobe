"""ioctl request-number arithmetic.

Every Linux ioctl is addressed by a single integer packing a
direction, a magic byte, a command number and the argument size.
Encoding them here beats sprinkling constants like ``0x40206B00``
through the device modules, and lets a request whose size depends on
the call — ``SPI_IOC_MESSAGE(n)`` — be built at the point of use.
"""

import sys

# Every module in this package speaks a Linux-specific ABI. Failing at
# import is what lets `_import_standard_enumerators` drop the whole
# family on other platforms.
if sys.platform != "linux":
    raise ImportError(f"acrobe.adapter.linux not supported on {sys.platform}")


class Ioctl:
    """Encoder for the ``asm-generic`` ioctl numbering.

    Covers x86, arm, arm64 and riscv. powerpc, mips, sparc and alpha
    use a different direction encoding and a 13-bit size field; they
    would need their own table here.
    """

    NRBITS = 8
    TYPEBITS = 8
    SIZEBITS = 14

    NRSHIFT = 0
    TYPESHIFT = NRSHIFT + NRBITS
    SIZESHIFT = TYPESHIFT + TYPEBITS
    DIRSHIFT = SIZESHIFT + SIZEBITS

    NONE = 0
    WRITE = 1
    READ = 2

    @classmethod
    def encode(cls, direction: int, magic, nr: int, size: int) -> int:
        if size >= (1 << cls.SIZEBITS):
            raise ValueError(
                f"ioctl argument size {size} exceeds the "
                f"{cls.SIZEBITS}-bit size field")
        if isinstance(magic, str):
            magic = ord(magic)
        return ((direction << cls.DIRSHIFT)
                | (size << cls.SIZESHIFT)
                | (magic << cls.TYPESHIFT)
                | (nr << cls.NRSHIFT))

    @classmethod
    def none(cls, magic, nr: int) -> int:
        return cls.encode(cls.NONE, magic, nr, 0)

    @classmethod
    def read(cls, magic, nr: int, size: int) -> int:
        return cls.encode(cls.READ, magic, nr, size)

    @classmethod
    def write(cls, magic, nr: int, size: int) -> int:
        return cls.encode(cls.WRITE, magic, nr, size)

    @classmethod
    def read_write(cls, magic, nr: int, size: int) -> int:
        return cls.encode(cls.READ | cls.WRITE, magic, nr, size)
