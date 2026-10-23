"""A32 encoder checked against GNU as (-march=armv7-a -mfpu=vfpv3)."""

import pytest

from acrobe.component.arm.a32 import A32, Barrier, Cond, DpOp, PsrMask, VfpSysReg


VECTORS = [
    ("mov r0, r0", A32.nop(), 0xe1a00000),
    ("mov r3, r12", A32.mov(3, 12), 0xe1a0300c),
    ("mov r1, #0xff000000", A32.mov_imm(1, 0xff000000), 0xe3a014ff),
    ("mvn r2, #0", A32.mvn_imm(2, 0), 0xe3e02000),
    ("orr r4, r5, #0x1c0", A32.orr_imm(4, 5, 0x1c0), 0xe3854d07),
    ("bic r0, r0, #0x1f", A32.bic_imm(0, 0, 0x1f), 0xe3c0001f),
    ("add r7, r7, #4", A32.add_imm(7, 7, 4), 0xe2877004),
    ("subne r1, r2, #0x3fc", A32.sub_imm(1, 2, 0x3fc, cond=Cond.NE), 0x12421fff),
    ("ldmia r0, {r1-r3, r14}", A32.ldmia(0, [1, 2, 3, 14]), 0xe890400e),
    ("ldmia r0!, {r1}", A32.ldmia(0, [1], writeback=True), 0xe8b00002),
    ("stmia r13, {r0-r15}", A32.stmia(13, range(16)), 0xe88dffff),
    ("stmia r0, {r1-r14}^", A32.stmia(0, range(1, 15), user=True), 0xe8c07ffe),
    ("ldmia r0, {r15}^", A32.ldmia(0, [15], user=True), 0xe8d08000),
    ("ldr r1, [r0]", A32.ldr(1, 0), 0xe5901000),
    ("ldr r1, [r0, #-4]", A32.ldr(1, 0, -4), 0xe5101004),
    ("ldr r1, [r0], #4", A32.ldr(1, 0, 4, post_index=True), 0xe4901004),
    ("str r2, [r3, #0xfff]", A32.str(2, 3, 0xfff), 0xe5832fff),
    ("ldrb r1, [r0], #1", A32.ldrb(1, 0, 1, post_index=True), 0xe4d01001),
    ("strb r1, [r0, #-1]", A32.strb(1, 0, -1), 0xe5401001),
    ("ldrh r1, [r0], #2", A32.ldrh(1, 0, 2, post_index=True), 0xe0d010b2),
    ("strh r1, [r0, #0xfe]", A32.strh(1, 0, 0xfe), 0xe1c01fbe),
    ("ldrh r1, [r0, #-0x12]", A32.ldrh(1, 0, -0x12), 0xe15011b2),
    ("mrs r0, cpsr", A32.mrs(0), 0xe10f0000),
    ("mrs r5, spsr", A32.mrs(5, spsr=True), 0xe14f5000),
    ("msr cpsr_fsxc, r0", A32.msr(0), 0xe12ff000),
    ("msr spsr_fsxc, r1", A32.msr(1, spsr=True), 0xe16ff001),
    ("msr cpsr_c, r2", A32.msr(2, PsrMask.C), 0xe121f002),
    ("msr cpsr_c, #0xd3", A32.msr_imm(0xd3, PsrMask.C), 0xe321f0d3),
    ("mrc p15, 0, r0, c1, c0, 0", A32.mrc(15, 0, 0, 1, 0, 0), 0xee110f10),
    ("mcr p15, 0, r0, c7, c10, 4", A32.mcr(15, 0, 0, 7, 10, 4), 0xee070f9a),
    ("mcr p14, 0, r1, c0, c5, 0", A32.mcr(14, 0, 1, 0, 5, 0), 0xee001e15),
    ("mrc p15, 3, r2, c15, c12, 7", A32.mrc(15, 3, 2, 15, 12, 7), 0xee7f2ffc),
    ("ldc p14, c5, [r0], #4", A32.ldc(14, 5, 0, 4, post_index=True), 0xecb05e01),
    ("stc p14, c5, [r0], #4", A32.stc(14, 5, 0, 4, post_index=True), 0xeca05e01),
    ("ldc p14, c5, [r0, #-8]", A32.ldc(14, 5, 0, -8), 0xed105e02),
    ("bx lr", A32.bx(A32.LR), 0xe12fff1e),
    ("bkpt #0", A32.bkpt(), 0xe1200070),
    ("bkpt #0xabcd", A32.bkpt(0xabcd), 0xe12abc7d),
    ("dsb sy", A32.dsb(), 0xf57ff04f),
    ("dmb ish", A32.dmb(Barrier.ISH), 0xf57ff05b),
    ("isb", A32.isb(), 0xf57ff06f),
    ("vmrs r0, fpscr", A32.vmrs(0), 0xeef10a10),
    ("vmrs r1, fpexc", A32.vmrs(1, VfpSysReg.FPEXC), 0xeef81a10),
    ("vmsr fpscr, r2", A32.vmsr(2), 0xeee12a10),
    ("vmsr fpexc, r3", A32.vmsr(3, VfpSysReg.FPEXC), 0xeee83a10),
    ("vmov r0, r1, d0", A32.vmov_from_d(0, 1, 0), 0xec510b10),
    ("vmov r2, r3, d17", A32.vmov_from_d(2, 3, 17), 0xec532b31),
    ("vmov d31, r4, r5", A32.vmov_to_d(31, 4, 5), 0xec454b3f),
]


@pytest.mark.parametrize("text,got,expected", VECTORS, ids=[v[0] for v in VECTORS])
def test_encoding(text, got, expected):
    assert got == expected, f"{text}: {got:#010x} != {expected:#010x}"


def test_branch():
    # Self-loop: "b ." encodes offset -8.
    assert A32.b(0) == 0xeafffffe
    assert A32.b(8) == 0xea000000
    assert A32.b(-4, link=True) == 0xebfffffd


def test_dp_generic():
    assert A32.dp_reg(DpOp.CMP, 0, 1, 2, s=True) == 0xe1510002


@pytest.mark.parametrize("value", [0x101, 0x12345678, 0xff1])
def test_rotated_imm_rejects(value):
    with pytest.raises(ValueError):
        A32.rotated_imm(value)


def test_range_checks():
    with pytest.raises(ValueError):
        A32.ldr(0, 1, 0x1000)
    with pytest.raises(ValueError):
        A32.ldrh(0, 1, 0x100)
    with pytest.raises(ValueError):
        A32.mcr(15, 0, 0, 0, 16)
    with pytest.raises(ValueError):
        A32.ldmia(0, [])
    with pytest.raises(ValueError):
        A32.ldc(14, 5, 0, 2)


def test_psr():
    from acrobe.component.arm.a32_state import Mode, Psr
    p = Psr(0x600001d3)
    assert p.mode is Mode.SVC
    assert p.I and p.F and p.A and not p.T
    assert p.Z and p.C and not p.N
    p.IT = 0x2d
    assert p.IT == 0x2d
    assert int(p) == 0x600001d3 | (0x2d >> 2) << 10 | (0x2d & 3) << 25
    p.mode = Mode.FIQ
    assert int(p) & 0x1f == 0x11


def test_banking():
    from acrobe.component.arm.a32_state import Mode
    assert list(Mode.FIQ.banked_registers()) == list(range(8, 15))
    assert list(Mode.SVC.banked_registers()) == [13, 14]
    assert list(Mode.HYP.banked_registers()) == [13]
    assert not Mode.SYS.banked_registers()
    assert Mode.SYS.bank is Mode.USR
    assert not Mode.SYS.has_spsr and Mode.ABT.has_spsr
