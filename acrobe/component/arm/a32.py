"""A32 (ARM state) instruction encoder.

Covers the subset a debugger injects into a halted core through
instruction stuffing: register moves, block and single transfers,
PSR access, coprocessor moves, branches, barriers and VFP
transfers. Every encoder returns the 32-bit instruction word.

Encodings follow the ARM Architecture Reference Manual (ARMv7-A/R,
section A8). Instructions introduced after ARMv5TE (barriers, VFP
system register moves beyond FPSCR/FPEXC) are marked as such; it is
the caller's job to pick instructions its core implements.
"""

import enum


class Cond(enum.IntEnum):
    EQ = 0x0
    NE = 0x1
    CS = 0x2
    CC = 0x3
    MI = 0x4
    PL = 0x5
    VS = 0x6
    VC = 0x7
    HI = 0x8
    LS = 0x9
    GE = 0xa
    LT = 0xb
    GT = 0xc
    LE = 0xd
    AL = 0xe


class DpOp(enum.IntEnum):
    AND = 0x0
    EOR = 0x1
    SUB = 0x2
    RSB = 0x3
    ADD = 0x4
    ADC = 0x5
    SBC = 0x6
    RSC = 0x7
    TST = 0x8
    TEQ = 0x9
    CMP = 0xa
    CMN = 0xb
    ORR = 0xc
    MOV = 0xd
    BIC = 0xe
    MVN = 0xf


class PsrMask(enum.IntFlag):
    """MSR field mask (`<fields>` in `MSR CPSR_<fields>`)."""
    C = 0x1
    X = 0x2
    S = 0x4
    F = 0x8
    ALL = 0xf


class Barrier(enum.IntEnum):
    """Barrier option field of DMB / DSB."""
    SY = 0xf
    ST = 0xe
    ISH = 0xb
    ISHST = 0xa
    NSH = 0x7
    NSHST = 0x6
    OSH = 0x3
    OSHST = 0x2


class VfpSysReg(enum.IntEnum):
    """`reg` field of VMRS / VMSR."""
    FPSID = 0x0
    FPSCR = 0x1
    MVFR1 = 0x6
    MVFR0 = 0x7
    FPEXC = 0x8


class A32:
    SP = 13
    LR = 14
    PC = 15

    @staticmethod
    def rotated_imm(value: int) -> int:
        """Encode `value` as a 12-bit modified immediate
        (8-bit constant rotated right by twice a 4-bit amount).

        Raises ValueError if `value` has no such encoding."""
        value &= 0xffffffff
        for rot in range(16):
            imm8 = ((value << (2 * rot)) | (value >> (32 - 2 * rot))) & 0xffffffff
            if imm8 < 0x100:
                return (rot << 8) | imm8
        raise ValueError(f"{value:#010x} is not a rotated 8-bit immediate")

    @staticmethod
    def register_list(regs) -> int:
        mask = 0
        for r in regs:
            if not 0 <= r < 16:
                raise ValueError(f"Invalid register r{r}")
            mask |= 1 << r
        if not mask:
            raise ValueError("Empty register list")
        return mask

    @staticmethod
    def __reg(r: int) -> int:
        if not 0 <= r < 16:
            raise ValueError(f"Invalid register r{r}")
        return r

    @classmethod
    def __dp(cls, op: DpOp, rd: int, rn: int, operand2: int, *,
             imm: bool, s: bool, cond: Cond) -> int:
        return (cond << 28) | (int(imm) << 25) | (op << 21) | (int(s) << 20) \
            | (cls.__reg(rn) << 16) | (cls.__reg(rd) << 12) | operand2

    @classmethod
    def dp_imm(cls, op: DpOp, rd: int, rn: int, value: int, *,
               s: bool = False, cond: Cond = Cond.AL) -> int:
        """Data-processing instruction with a modified immediate
        operand: `<op> rd, rn, #value`."""
        return cls.__dp(op, rd, rn, cls.rotated_imm(value),
                        imm=True, s=s, cond=cond)

    @classmethod
    def dp_reg(cls, op: DpOp, rd: int, rn: int, rm: int, *,
               s: bool = False, cond: Cond = Cond.AL) -> int:
        """Data-processing instruction with an unshifted register
        operand: `<op> rd, rn, rm`."""
        return cls.__dp(op, rd, rn, cls.__reg(rm),
                        imm=False, s=s, cond=cond)

    @classmethod
    def mov(cls, rd: int, rm: int, *, cond: Cond = Cond.AL) -> int:
        return cls.dp_reg(DpOp.MOV, rd, 0, rm, cond=cond)

    @classmethod
    def mov_imm(cls, rd: int, value: int, *, cond: Cond = Cond.AL) -> int:
        return cls.dp_imm(DpOp.MOV, rd, 0, value, cond=cond)

    @classmethod
    def mvn_imm(cls, rd: int, value: int, *, cond: Cond = Cond.AL) -> int:
        return cls.dp_imm(DpOp.MVN, rd, 0, value, cond=cond)

    @classmethod
    def orr_imm(cls, rd: int, rn: int, value: int, *, cond: Cond = Cond.AL) -> int:
        return cls.dp_imm(DpOp.ORR, rd, rn, value, cond=cond)

    @classmethod
    def bic_imm(cls, rd: int, rn: int, value: int, *, cond: Cond = Cond.AL) -> int:
        return cls.dp_imm(DpOp.BIC, rd, rn, value, cond=cond)

    @classmethod
    def add_imm(cls, rd: int, rn: int, value: int, *, cond: Cond = Cond.AL) -> int:
        return cls.dp_imm(DpOp.ADD, rd, rn, value, cond=cond)

    @classmethod
    def sub_imm(cls, rd: int, rn: int, value: int, *, cond: Cond = Cond.AL) -> int:
        return cls.dp_imm(DpOp.SUB, rd, rn, value, cond=cond)

    @classmethod
    def nop(cls) -> int:
        """`mov r0, r0`: the architectural no-op on cores without
        the ARMv6K NOP hint, and what EmbeddedICE pipelines expect
        between injected instructions."""
        return cls.mov(0, 0)

    @classmethod
    def __ldstm(cls, load: bool, rn: int, regs, *, writeback: bool,
                user: bool, cond: Cond) -> int:
        # Increment-after only: P=0, U=1.
        return (cond << 28) | (0b100 << 25) | (1 << 23) | (int(user) << 22) \
            | (int(writeback) << 21) | (int(load) << 20) \
            | (cls.__reg(rn) << 16) | cls.register_list(regs)

    @classmethod
    def ldmia(cls, rn: int, regs, *, writeback: bool = False,
              user: bool = False, cond: Cond = Cond.AL) -> int:
        """`ldmia rn[!], {regs}[^]`. `user=True` loads the User-mode
        bank (or restores CPSR from SPSR when `regs` holds pc)."""
        return cls.__ldstm(True, rn, regs, writeback=writeback,
                           user=user, cond=cond)

    @classmethod
    def stmia(cls, rn: int, regs, *, writeback: bool = False,
              user: bool = False, cond: Cond = Cond.AL) -> int:
        """`stmia rn[!], {regs}[^]`. `user=True` stores the
        User-mode bank."""
        return cls.__ldstm(False, rn, regs, writeback=writeback,
                           user=user, cond=cond)

    @staticmethod
    def __offset(offset: int, limit: int) -> tuple[int, int]:
        up = offset >= 0
        magnitude = abs(offset)
        if magnitude >= limit:
            raise ValueError(f"Offset {offset} out of range")
        return int(up), magnitude

    @classmethod
    def __ldst(cls, load: bool, byte: bool, rt: int, rn: int, offset: int,
               post_index: bool, cond: Cond) -> int:
        up, imm12 = cls.__offset(offset, 1 << 12)
        p = 0 if post_index else 1
        return (cond << 28) | (0b010 << 25) | (p << 24) | (up << 23) \
            | (int(byte) << 22) | (int(load) << 20) \
            | (cls.__reg(rn) << 16) | (cls.__reg(rt) << 12) | imm12

    @classmethod
    def __ldsth(cls, load: bool, rt: int, rn: int, offset: int,
                post_index: bool, cond: Cond) -> int:
        up, imm8 = cls.__offset(offset, 1 << 8)
        p = 0 if post_index else 1
        return (cond << 28) | (p << 24) | (up << 23) | (1 << 22) \
            | (int(load) << 20) | (cls.__reg(rn) << 16) \
            | (cls.__reg(rt) << 12) | ((imm8 >> 4) << 8) | (0b1011 << 4) \
            | (imm8 & 0xf)

    # Single transfers. `post_index=False` is `[rn, #offset]` without
    # writeback; `post_index=True` is `[rn], #offset`, which always
    # writes back.

    @classmethod
    def ldr(cls, rt: int, rn: int, offset: int = 0, *,
            post_index: bool = False, cond: Cond = Cond.AL) -> int:
        return cls.__ldst(True, False, rt, rn, offset, post_index, cond)

    @classmethod
    def str(cls, rt: int, rn: int, offset: int = 0, *,
            post_index: bool = False, cond: Cond = Cond.AL) -> int:
        return cls.__ldst(False, False, rt, rn, offset, post_index, cond)

    @classmethod
    def ldrb(cls, rt: int, rn: int, offset: int = 0, *,
             post_index: bool = False, cond: Cond = Cond.AL) -> int:
        return cls.__ldst(True, True, rt, rn, offset, post_index, cond)

    @classmethod
    def strb(cls, rt: int, rn: int, offset: int = 0, *,
             post_index: bool = False, cond: Cond = Cond.AL) -> int:
        return cls.__ldst(False, True, rt, rn, offset, post_index, cond)

    @classmethod
    def ldrh(cls, rt: int, rn: int, offset: int = 0, *,
             post_index: bool = False, cond: Cond = Cond.AL) -> int:
        return cls.__ldsth(True, rt, rn, offset, post_index, cond)

    @classmethod
    def strh(cls, rt: int, rn: int, offset: int = 0, *,
             post_index: bool = False, cond: Cond = Cond.AL) -> int:
        return cls.__ldsth(False, rt, rn, offset, post_index, cond)

    @classmethod
    def mrs(cls, rd: int, *, spsr: bool = False, cond: Cond = Cond.AL) -> int:
        return (cond << 28) | 0x010f0000 | (int(spsr) << 22) | (cls.__reg(rd) << 12)

    @classmethod
    def msr(cls, rm: int, mask: PsrMask = PsrMask.ALL, *, spsr: bool = False,
            cond: Cond = Cond.AL) -> int:
        return (cond << 28) | 0x0120f000 | (int(spsr) << 22) \
            | (int(mask) << 16) | cls.__reg(rm)

    @classmethod
    def msr_imm(cls, value: int, mask: PsrMask, *, spsr: bool = False,
                cond: Cond = Cond.AL) -> int:
        return (cond << 28) | 0x0320f000 | (int(spsr) << 22) \
            | (int(mask) << 16) | cls.rotated_imm(value)

    @staticmethod
    def __cp_check(coproc: int, opc1: int, crn: int, crm: int, opc2: int):
        if not (0 <= coproc < 16 and 0 <= opc1 < 8 and 0 <= crn < 16
                and 0 <= crm < 16 and 0 <= opc2 < 8):
            raise ValueError("Coprocessor operand out of range")

    @classmethod
    def mrc(cls, coproc: int, opc1: int, rt: int, crn: int, crm: int,
            opc2: int = 0, *, cond: Cond = Cond.AL) -> int:
        """`mrc p<coproc>, opc1, rt, c<crn>, c<crm>, opc2`."""
        cls.__cp_check(coproc, opc1, crn, crm, opc2)
        return (cond << 28) | 0x0e100010 | (opc1 << 21) | (crn << 16) \
            | (cls.__reg(rt) << 12) | (coproc << 8) | (opc2 << 5) | crm

    @classmethod
    def mcr(cls, coproc: int, opc1: int, rt: int, crn: int, crm: int,
            opc2: int = 0, *, cond: Cond = Cond.AL) -> int:
        """`mcr p<coproc>, opc1, rt, c<crn>, c<crm>, opc2`."""
        cls.__cp_check(coproc, opc1, crn, crm, opc2)
        return (cond << 28) | 0x0e000010 | (opc1 << 21) | (crn << 16) \
            | (cls.__reg(rt) << 12) | (coproc << 8) | (opc2 << 5) | crm

    @classmethod
    def __ldstc(cls, load: bool, coproc: int, crd: int, rn: int, offset: int,
                post_index: bool, cond: Cond) -> int:
        if offset % 4:
            raise ValueError(f"Offset {offset} is not word-aligned")
        if not (0 <= coproc < 16 and 0 <= crd < 16):
            raise ValueError("Coprocessor operand out of range")
        up, imm8 = cls.__offset(offset // 4, 1 << 8)
        p, w = (0, 1) if post_index else (1, 0)
        return (cond << 28) | (0b110 << 25) | (p << 24) | (up << 23) \
            | (w << 21) | (int(load) << 20) | (cls.__reg(rn) << 16) \
            | (crd << 12) | (coproc << 8) | imm8

    @classmethod
    def ldc(cls, coproc: int, crd: int, rn: int, offset: int = 0, *,
            post_index: bool = False, cond: Cond = Cond.AL) -> int:
        """`ldc p<coproc>, c<crd>, [rn, #offset]` or, with
        `post_index`, `[rn], #offset`. `offset` is in bytes."""
        return cls.__ldstc(True, coproc, crd, rn, offset, post_index, cond)

    @classmethod
    def stc(cls, coproc: int, crd: int, rn: int, offset: int = 0, *,
            post_index: bool = False, cond: Cond = Cond.AL) -> int:
        """`stc p<coproc>, c<crd>, [rn, #offset]` or, with
        `post_index`, `[rn], #offset`. `offset` is in bytes."""
        return cls.__ldstc(False, coproc, crd, rn, offset, post_index, cond)

    @staticmethod
    def b(offset: int, *, link: bool = False, cond: Cond = Cond.AL) -> int:
        """Branch to `offset` bytes from the branch instruction's own
        address (the PC+8 read-ahead is accounted for here)."""
        if offset % 4:
            raise ValueError(f"Branch offset {offset} is not word-aligned")
        imm = (offset - 8) >> 2
        if not -(1 << 23) <= imm < (1 << 23):
            raise ValueError(f"Branch offset {offset} out of range")
        return (cond << 28) | (0b101 << 25) | (int(link) << 24) | (imm & 0xffffff)

    @classmethod
    def bx(cls, rm: int, *, cond: Cond = Cond.AL) -> int:
        """ARMv5T+."""
        return (cond << 28) | 0x012fff10 | cls.__reg(rm)

    @staticmethod
    def bkpt(imm: int = 0) -> int:
        """ARMv5T+. Unconditional."""
        if not 0 <= imm < (1 << 16):
            raise ValueError(f"BKPT immediate {imm} out of range")
        return 0xe1200070 | ((imm >> 4) << 8) | (imm & 0xf)

    @staticmethod
    def dsb(option: Barrier = Barrier.SY) -> int:
        """ARMv7+. Earlier cores use the CP15 c7 equivalents."""
        return 0xf57ff040 | option

    @staticmethod
    def dmb(option: Barrier = Barrier.SY) -> int:
        """ARMv7+. Earlier cores use the CP15 c7 equivalents."""
        return 0xf57ff050 | option

    @staticmethod
    def isb() -> int:
        """ARMv7+. Earlier cores use the CP15 c7 equivalents."""
        return 0xf57ff06f

    @classmethod
    def vmrs(cls, rt: int, reg: VfpSysReg = VfpSysReg.FPSCR, *,
             cond: Cond = Cond.AL) -> int:
        """`vmrs rt, <reg>`. rt must not be pc (that form transfers
        FPSCR flags to APSR and is not exposed here)."""
        if rt == cls.PC:
            raise ValueError("vmrs to pc not supported")
        return (cond << 28) | 0x0ef00a10 | (reg << 16) | (cls.__reg(rt) << 12)

    @classmethod
    def vmsr(cls, rt: int, reg: VfpSysReg = VfpSysReg.FPSCR, *,
             cond: Cond = Cond.AL) -> int:
        """`vmsr <reg>, rt`."""
        return (cond << 28) | 0x0ee00a10 | (reg << 16) | (cls.__reg(rt) << 12)

    @classmethod
    def __vmov_d(cls, to_core: bool, rt: int, rt2: int, dm: int,
                 cond: Cond) -> int:
        if not 0 <= dm < 32:
            raise ValueError(f"Invalid register d{dm}")
        return (cond << 28) | 0x0c400b10 | (int(to_core) << 20) \
            | (cls.__reg(rt2) << 16) | (cls.__reg(rt) << 12) \
            | ((dm >> 4) << 5) | (dm & 0xf)

    @classmethod
    def vmov_from_d(cls, rt: int, rt2: int, dm: int, *,
                    cond: Cond = Cond.AL) -> int:
        """`vmov rt, rt2, d<dm>`: rt gets the low word."""
        return cls.__vmov_d(True, rt, rt2, dm, cond)

    @classmethod
    def vmov_to_d(cls, dm: int, rt: int, rt2: int, *,
                  cond: Cond = Cond.AL) -> int:
        """`vmov d<dm>, rt, rt2`: rt provides the low word."""
        return cls.__vmov_d(False, rt, rt2, dm, cond)
