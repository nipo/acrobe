"""ARM A/R-profile processor state: modes, PSR layout, register banking."""

import enum

from acrobe.bitfield import Bitfield, BooleanField, EnumField, Field


class Mode(enum.IntEnum):
    USR = 0x10
    FIQ = 0x11
    IRQ = 0x12
    SVC = 0x13
    MON = 0x16
    ABT = 0x17
    HYP = 0x1a
    UND = 0x1b
    SYS = 0x1f

    @property
    def privileged(self) -> bool:
        return self is not Mode.USR

    @property
    def has_spsr(self) -> bool:
        return self not in (Mode.USR, Mode.SYS)

    @property
    def bank(self) -> "Mode":
        """Mode whose registers this mode's banked registers alias;
        SYS shares the User bank."""
        return Mode.USR if self is Mode.SYS else self

    def banked_registers(self) -> range:
        """Core registers that are private to this mode's bank.

        HYP banks SP only: its return address lives in ELR_hyp and
        LR is the User-mode one."""
        match self.bank:
            case Mode.USR:
                return range(0)
            case Mode.FIQ:
                return range(8, 15)
            case Mode.HYP:
                return range(13, 14)
            case _:
                return range(13, 15)

    def is_banked(self, r: int) -> bool:
        return r in self.banked_registers()


class Psr(Bitfield):
    """CPSR / SPSR."""
    mode = EnumField(0, 5, Mode)
    T = BooleanField(5)
    F = BooleanField(6)
    I = BooleanField(7)
    A = BooleanField(8)
    E = BooleanField(9)
    IT_high = Field(10, 6)
    GE = Field(16, 4)
    J = BooleanField(24)
    IT_low = Field(25, 2)
    Q = BooleanField(27)
    V = BooleanField(28)
    C = BooleanField(29)
    Z = BooleanField(30)
    N = BooleanField(31)

    @property
    def IT(self) -> int:
        return (self.IT_high << 2) | self.IT_low

    @IT.setter
    def IT(self, value: int):
        self.IT_high = value >> 2
        self.IT_low = value & 3

    @property
    def thumb(self) -> bool:
        return self.T and not self.J

    @property
    def jazelle(self) -> bool:
        return self.J and not self.T

    @property
    def thumbee(self) -> bool:
        return self.J and self.T
