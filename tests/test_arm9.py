"""ARM9E-S debug: scan chain packing and lowering."""

import asyncio

import pytest

from acrobe.bitstring import BitString
from acrobe.component.arm.a32 import A32
from acrobe.component.arm.arm9.debug import (
    Arm9Debug, Cp15AccessIncomplete, IceRead,
)
from acrobe.component.arm.arm9.embedded_ice import IceRegister, WatchpointOffset
from acrobe.component.arm.arm9.scan_chain import (
    Cp15Chain, DebugChain, IceChain, ScanChain,
)
from acrobe.component.arm.arm9.tap import Arm9Tap
from acrobe.protocol.jtag import Tap, _TapRun, _TapShift


class RecordingArm9Tap(Arm9Tap):
    """Records TAP-level ops and answers reading shifts from a
    queue of TDO values (zero when empty)."""

    def __init__(self):
        super().__init__(idcode=0x07926f0f, irlen=4, name="rec")
        self.ops = []
        self.tdo_queue = []

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, _TapShift):
                tdi = int(op.tdi) if op.tdi is not None else None
                self.ops.append(("shift", op.ir_value, tdi, op.read_tdo))
                if op.read_tdo:
                    value = self.tdo_queue.pop(0) if self.tdo_queue else 0
                    future.set_result(BitString(value, len(op.tdi)))
                else:
                    future.set_result(None)
            elif isinstance(op, _TapRun):
                self.ops.append(("run", op.cycles))
                future.set_result(None)


def make_debug():
    tap = RecordingArm9Tap()
    debug = Arm9Debug()
    tap.child_add(debug)
    return tap, debug


SCAN_N, RESTART, INTEST = 0x2, 0x4, 0xc


def test_registered():
    assert Arm9Tap in Tap.db.get(0x07926f0f)
    assert Arm9Tap in Tap.db.get(0x17926f0f)


def test_debug_chain_pack():
    v = DebugChain.pack(0x00000001, 0xdeadbeef, sysspeed=True)
    assert v & 0xffffffff == 0xdeadbeef
    assert v & DebugChain.SYSSPEED
    assert v >> DebugChain.INSTR_LSB == 0x80000000
    assert v < 1 << DebugChain.LENGTH


def test_ice_chain_pack():
    assert IceChain.pack(True, 0x05, 0x1234) == (1 << 37) | (5 << 32) | 0x1234


def test_cp15_chain_pack():
    # mrc p15, 2, rX, c9, c13, 5
    v = Cp15Chain.pack(False, 2, 9, 13, 5)
    assert v == (2 << 44) | (5 << 41) | (9 << 37) | (13 << 33) | (1 << 32)


def test_watchpoint_address():
    assert IceRegister.watchpoint(1, WatchpointOffset.CTRL_MASK) == 0x15


async def test_ice_reads_pipeline():
    tap, debug = make_debug()
    tap.tdo_queue = [0x11, 0x22]
    f1 = asyncio.ensure_future(debug.ice_read(IceRegister.DEBUG_STATUS))
    f2 = asyncio.ensure_future(debug.ice_read(IceRegister.COMMS_CTRL))
    assert await f1 == 0x11
    assert await f2 == 0x22
    assert tap.ops == [
        ("shift", SCAN_N, ScanChain.EMBEDDED_ICE, False),
        ("shift", INTEST, IceChain.pack(False, 0x01), False),
        ("shift", INTEST, IceChain.pack(False, 0x04), True),
        ("shift", INTEST, IceChain.pack(False, 0x01), True),
    ]


async def test_chain_selection_is_cached():
    tap, debug = make_debug()
    await debug.ice_write(IceRegister.DEBUG_CTRL, 2)
    await debug.ice_write(IceRegister.DEBUG_CTRL, 0)
    assert [op for op in tap.ops if op[1] == SCAN_N] == [
        ("shift", SCAN_N, ScanChain.EMBEDDED_ICE, False)]
    debug.chain_forget()
    await debug.ice_write(IceRegister.DEBUG_CTRL, 0)
    assert tap.ops[-2] == ("shift", SCAN_N, ScanChain.EMBEDDED_ICE, False)


async def test_ice_read_flushed_before_chain_switch():
    tap, debug = make_debug()
    tap.tdo_queue = [0x9]
    status = debug.post(IceRead(IceRegister.DEBUG_STATUS))
    await debug.nop()
    assert await status == 0x9
    assert tap.ops[2] == ("shift", INTEST, IceChain.pack(False, 0x01), True)
    assert tap.ops[3] == ("shift", SCAN_N, ScanChain.DEBUG, False)


async def test_cp15_read():
    tap, debug = make_debug()
    tap.tdo_queue = [Cp15Chain.ACCESS | 0x41069265]
    assert await debug.cp15_read(0, 0, 0, 0) == 0x41069265
    assert tap.ops[1:] == [
        ("shift", INTEST, Cp15Chain.pack(False, 0, 0, 0, 0), False),
        ("shift", INTEST, 0, True),
    ]


async def test_cp15_incomplete():
    tap, debug = make_debug()
    tap.tdo_queue = [0x1234]
    with pytest.raises(Cp15AccessIncomplete):
        await debug.cp15_write(0, 1, 0, 0, 0x78)


async def test_regs_read():
    tap, debug = make_debug()
    tap.tdo_queue = [0xa, 0xb]
    values = await asyncio.gather(*debug.post_regs_read([3, 1]))
    assert values == [0xa, 0xb]
    instrs = [DebugChain.pack(i, 0, False) for i in
              (A32.stmia(0, [1, 3]), A32.nop(), A32.nop(), A32.nop(), A32.nop())]
    assert [op[2] for op in tap.ops[1:]] == instrs
    assert [op[3] for op in tap.ops[1:]] == [False, False, False, True, True]


async def test_regs_write():
    tap, debug = make_debug()
    await debug.post_regs_write({2: 0x22, 0: 0x00})
    tdis = [op[2] for op in tap.ops[1:]]
    assert tdis == [
        DebugChain.pack(A32.ldmia(0, [0, 2]), 0, False),
        DebugChain.pack(A32.nop(), 0, False),
        DebugChain.pack(A32.nop(), 0, False),
        DebugChain.pack(A32.nop(), 0x00, False),
        DebugChain.pack(A32.nop(), 0x22, False),
        DebugChain.pack(A32.nop(), 0, False),
    ]
    with pytest.raises(ValueError):
        debug.post_regs_write({15: 0})


async def test_psr_write_uses_no_register():
    tap, debug = make_debug()
    await debug.post_psr_write(0x600000d3)
    instrs = [A32.msr_imm(0xd3, 1), A32.msr_imm(0, 2), A32.msr_imm(0, 4),
              A32.nop(), A32.nop(), A32.msr_imm(0x60000000, 8)]
    tdis = [op[2] for op in tap.ops[1:7]]
    assert tdis == [DebugChain.pack(i, 0, False) for i in instrs]


async def test_system_speed_waits_completion():
    tap, debug = make_debug()
    tap.tdo_queue = [0x1, 0x9]
    status = await debug.system_speed(
        A32.ldmia(14, [0, 1], writeback=True))
    assert status.syscomp and status.dbgack
    assert [op[:3] for op in tap.ops[1:]] == [
        ("shift", INTEST, DebugChain.pack(
            A32.ldmia(14, [0, 1], writeback=True), 0, False)),
        ("shift", INTEST, DebugChain.pack(A32.nop(), 0, True)),
        ("shift", RESTART, None),
        ("run", 1),
        ("shift", SCAN_N, ScanChain.EMBEDDED_ICE),
        ("shift", INTEST, IceChain.pack(False, 0x01)),
        ("shift", INTEST, IceChain.pack(False, 0x01)),
        ("shift", INTEST, IceChain.pack(False, 0x01)),
        ("shift", INTEST, IceChain.pack(False, 0x01)),
    ]


async def test_load_words():
    tap, debug = make_debug()
    tap.tdo_queue = [0x9, 0x11, 0x22]
    assert await debug.load_words(2) == [0x11, 0x22]
    assert tap.ops[1][2] == DebugChain.pack(
        A32.ldmia(0, [1, 2], writeback=True), 0, False)
    assert tap.ops[8] == ("shift", SCAN_N, ScanChain.DEBUG, False)
    assert tap.ops[9][2] == DebugChain.pack(A32.stmia(0, [1, 2]), 0, False)


async def test_store_halfwords():
    tap, debug = make_debug()
    tap.tdo_queue = [0x9, 0x9]
    await debug.store_units(2, [0x1234, 0x5678])
    instrs = [op[2] >> DebugChain.INSTR_LSB for op in tap.ops
              if op[:2] == ("shift", INTEST) and op[2] & DebugChain.SYSSPEED]
    assert len(instrs) == 2
    shifted = [op[2] for op in tap.ops if op[:2] == ("shift", INTEST)]
    assert DebugChain.pack(A32.strh(1, 0, 2, post_index=True), 0, False) \
        in shifted
    assert DebugChain.pack(A32.strh(2, 0, 2, post_index=True), 0, False) \
        in shifted
    with pytest.raises(ValueError):
        await debug.store_units(1, [0] * 15)
