import asyncio
import pytest
from crobe_async.component.arm.cortex import (
    Cortex, CpuState, HaltCause, CortexReg, ScsRegs,
    C_DEBUGEN, C_HALT, S_HALT, S_REGRDY,
    DHCSR_KEY,
)
from crobe_async.component.arm.ap import MemAp
from crobe_async.component.arm.dp import SwDp
from crobe_async.protocol.swd import Read as SwdRead, Write as SwdWrite
from crobe_async.engine import Batcher


class MockSwdInterface(Batcher):
    """SWD interface mock with configurable memory-mapped register responses."""

    def __init__(self):
        super().__init__()
        self.ops = []
        self._memory = {}  # addr -> value (simulates target memory)
        self._default_read = 0

    def mem_write(self, addr, value):
        """Pre-populate a simulated memory location."""
        self._memory[addr] = value

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, SwdRead):
                op.data = self._default_read
            future.set_result(op)


class TestCpuState:
    def test_states(self):
        assert CpuState.RUN == 1
        assert CpuState.HALT == 2
        assert CpuState.LOCKUP == 3

    def test_halt_causes(self):
        assert HaltCause.DEBUGGER == 1
        assert HaltCause.BREAKPOINT == 2
        assert HaltCause.WATCHPOINT == 3


class TestCortexReg:
    def test_core_regs(self):
        assert CortexReg.R0 == 0
        assert CortexReg.SP == 13
        assert CortexReg.LR == 14
        assert CortexReg.PC == 15
        assert CortexReg.XPSR == 16


class TestScsRegs:
    def test_offsets(self):
        assert ScsRegs.CPUID == 0xd00
        assert ScsRegs.DHCSR == 0xdf0
        assert ScsRegs.DCRSR == 0xdf4
        assert ScsRegs.DCRDR == 0xdf8
        assert ScsRegs.DEMCR == 0xdfc
        assert ScsRegs.AIRCR == 0xd0c


class TestCortexConstruction:
    def test_init(self):
        iface = MockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)
        cortex = Cortex(memap)
        assert cortex.name == "cortex"

    def test_custom_base(self):
        iface = MockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)
        cortex = Cortex(memap, scs_base=0xf0000000)
        assert cortex._base == 0xf0000000

    def test_repr(self):
        iface = MockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)
        cortex = Cortex(memap)
        assert "Cortex" in repr(cortex)
        assert "0xe000e000" in repr(cortex)


class TestCortexDhcsrConstants:
    def test_dhcsr_key(self):
        assert DHCSR_KEY == 0xa05f0000

    def test_control_bits(self):
        assert C_DEBUGEN == 1
        assert C_HALT == 2

    def test_status_bits(self):
        assert S_HALT == (1 << 17)
        assert S_REGRDY == (1 << 16)
