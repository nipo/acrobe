"""ARM9E-S / ARM9EJ-S scan chain layouts behind INTEST.

The TAP multiplexes several scan chains behind one INTEST
instruction; SCAN_N selects which.
"""

import enum


class ScanChain(enum.IntEnum):
    DEBUG = 1
    EMBEDDED_ICE = 2
    EXTERNAL = 3
    CP15 = 15


class DebugChain:
    """Scan chain 1: data bus, control, instruction bus.

    The instruction field is shifted MSB first, so its value is
    bit-reversed in the DR word."""

    LENGTH = 67
    SYSSPEED = 1 << 34
    INSTR_LSB = 35

    @classmethod
    def pack(cls, instr: int, data: int, sysspeed: bool) -> int:
        instr = int(f"{instr & 0xffffffff:032b}"[::-1], 2)
        return (instr << cls.INSTR_LSB) | (cls.SYSSPEED if sysspeed else 0) \
            | (data & 0xffffffff)

    @staticmethod
    def data(tdo) -> int:
        return int(tdo) & 0xffffffff


class IceChain:
    """Scan chain 2: EmbeddedICE register access."""

    LENGTH = 38

    @staticmethod
    def pack(write: bool, register: int, data: int = 0) -> int:
        return (int(write) << 37) | ((register & 0x1f) << 32) \
            | (data & 0xffffffff)

    @staticmethod
    def data(tdo) -> int:
        return int(tdo) & 0xffffffff


class Cp15Chain:
    """Scan chain 15: CP15 register access."""

    LENGTH = 48
    ACCESS = 1 << 32

    @classmethod
    def pack(cls, write: bool, opc1: int, crn: int, crm: int, opc2: int,
             data: int = 0) -> int:
        if not (0 <= opc1 < 8 and 0 <= opc2 < 8
                and 0 <= crn < 16 and 0 <= crm < 16):
            raise ValueError("CP15 register address out of range")
        return (int(write) << 47) | (opc1 << 44) | (opc2 << 41) \
            | (crn << 37) | (crm << 33) | cls.ACCESS | (data & 0xffffffff)

    @classmethod
    def nop(cls) -> int:
        return 0

    @classmethod
    def completed(cls, tdo) -> bool:
        return bool(int(tdo) & cls.ACCESS)

    @staticmethod
    def data(tdo) -> int:
        return int(tdo) & 0xffffffff
