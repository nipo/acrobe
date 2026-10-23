"""ARM9E-S / ARM9EJ-S EmbeddedICE-RT register map (scan chain 2)."""

import enum

from ....bitfield import Bitfield, BooleanField, EnumField, Field


class IceRegister(enum.IntEnum):
    DEBUG_CTRL = 0x00
    DEBUG_STATUS = 0x01
    VECTOR_CATCH = 0x02
    COMMS_CTRL = 0x04
    COMMS_DATA = 0x05

    @staticmethod
    def watchpoint(unit: int, offset: "WatchpointOffset") -> int:
        if unit not in (0, 1):
            raise ValueError(f"No watchpoint unit {unit}")
        return 0x08 + 8 * unit + offset


class WatchpointOffset(enum.IntEnum):
    ADDR_VALUE = 0
    ADDR_MASK = 1
    DATA_VALUE = 2
    DATA_MASK = 3
    CTRL_VALUE = 4
    CTRL_MASK = 5


class DebugControl(Bitfield):
    dbgack = BooleanField(0)
    dbgrq = BooleanField(1)
    intdis = BooleanField(2)
    single_step = BooleanField(3)
    monitor = BooleanField(4)
    disable = BooleanField(5)


class MethodOfEntry(enum.IntEnum):
    NONE = 0
    WATCHPOINT0_BREAKPOINT = 1
    WATCHPOINT1_BREAKPOINT = 2
    BKPT = 3
    VECTOR_CATCH = 4
    EXTERNAL_BREAKPOINT = 5
    WATCHPOINT0 = 6
    WATCHPOINT1 = 7
    EXTERNAL_WATCHPOINT = 8
    INTERNAL_DBGRQ = 9
    EXTERNAL_DBGRQ = 10
    SYSTEM_SPEED_REENTRY = 11


class DebugStatus(Bitfield):
    dbgack = BooleanField(0)
    dbgrq = BooleanField(1)
    ifen = BooleanField(2)
    syscomp = BooleanField(3)
    tbit = BooleanField(4)
    jbit = BooleanField(5)
    moe = EnumField(6, 4, MethodOfEntry)


class VectorCatch(Bitfield):
    reset = BooleanField(0)
    undefined = BooleanField(1)
    swi = BooleanField(2)
    prefetch_abort = BooleanField(3)
    data_abort = BooleanField(4)
    irq = BooleanField(6)
    fiq = BooleanField(7)


class CommsControl(Bitfield):
    """Read-only from the debugger side; `r` is "core wrote,
    debugger has not read", `w` is "debugger wrote, core has not
    read"."""
    w = BooleanField(0)
    r = BooleanField(1)
    version = Field(28, 4)


class WatchpointControl(Bitfield):
    """Watchpoint control value / mask layout.

    In the matching mask registers, a set bit means *ignore* the
    corresponding value bit."""
    write = BooleanField(0)
    size = Field(1, 2)
    data = BooleanField(3)
    privileged = BooleanField(4)
    external = BooleanField(5)
    chain = BooleanField(6)
    range = BooleanField(7)
    enable = BooleanField(8)
