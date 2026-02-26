"""Tests for Puppet framework."""

import asyncio
import struct
import pytest
from crobe_async.puppet import Puppet, PuppetStub, Zone, ARM_M_TRAMPOLINE, ArmMPuppet
from crobe_async.component.arm.cortex import CpuState, CortexReg
from crobe_async.allocator import Range


class MockMemAp:
    """Mock MemAp with byte-level memory."""

    def __init__(self, base=0x20000000, size=0x10000):
        self._base = base
        self._memory = bytearray(size)

    async def mem_read(self, addr, size):
        off = addr - self._base
        return bytes(self._memory[off:off + size])

    async def mem_write(self, addr, data):
        off = addr - self._base
        self._memory[off:off + len(data)] = data


class MockCortex:
    """Mock Cortex that simulates halt after N state polls.

    When the CPU transitions from RUN to HALT, it writes return_value
    to R0 (simulating the function's return).
    """

    def __init__(self, halt_after=1, return_value=0):
        self._state = CpuState.HALT
        self._regs = {}
        self._halt_after = halt_after
        self._poll_count = 0
        self._resumed = False
        self.resume_count = 0
        self.halt_count = 0
        self.return_value = return_value

    async def state(self):
        if self._resumed:
            self._poll_count += 1
            if self._poll_count >= self._halt_after:
                self._state = CpuState.HALT
                self._resumed = False
                self._regs[CortexReg.R0] = self.return_value
        return self._state

    async def halt(self):
        self._state = CpuState.HALT
        self._resumed = False
        self.halt_count += 1

    async def resume(self):
        self._state = CpuState.RUN
        self._resumed = True
        self._poll_count = 0
        self.resume_count += 1

    async def reg_read(self, reg):
        return self._regs.get(reg, 0)

    async def reg_write(self, reg, value):
        self._regs[reg] = value

    async def regs_read(self, regs):
        return {r: self._regs.get(r, 0) for r in regs}

    async def regs_write(self, reg_values):
        self._regs.update(reg_values)


class TestZone:
    @pytest.mark.asyncio
    async def test_read_write(self):
        ap = MockMemAp()
        r = Range(0x20000100, 0x40)
        z = Zone(ap, r)

        assert z.address == 0x20000100
        assert z.end == 0x20000140
        assert z.size == 0x40

        await z.write(b"\xaa\xbb\xcc\xdd")
        data = await z.read(4)
        assert data == b"\xaa\xbb\xcc\xdd"

    @pytest.mark.asyncio
    async def test_read_write_with_offset(self):
        ap = MockMemAp()
        r = Range(0x20000100, 0x40)
        z = Zone(ap, r)

        await z.write(b"\xff", offset=0x10)
        data = await z.read(1, offset=0x10)
        assert data == b"\xff"


class TestPuppet:
    def _make_puppet(self, halt_after=1):
        cpu = MockCortex(halt_after=halt_after)
        ap = MockMemAp()
        puppet = Puppet(
            cpu, ap,
            ram_address=0x20000000, ram_size=0x4000,
            trampoline_code=ARM_M_TRAMPOLINE,
        )
        return puppet, cpu, ap

    @pytest.mark.asyncio
    async def test_allocate_and_free(self):
        puppet, _, _ = self._make_puppet()
        z = puppet.allocate(0x100)
        assert z.size == 0x100
        assert z.address >= 0x20000000
        puppet.free(z)

    @pytest.mark.asyncio
    async def test_prepare_sets_registers(self):
        puppet, cpu, _ = self._make_puppet()

        await puppet.prepare(0x08001000, 0x11, 0x22)

        # PC should be set to trampoline address
        assert cpu._regs[CortexReg.PC] == puppet._trampoline.address
        # SP should be set
        assert cpu._regs[CortexReg.SP] == puppet._stack_init
        # Args should be set
        assert cpu._regs[CortexReg.R0] == 0x11
        assert cpu._regs[CortexReg.R1] == 0x22

    @pytest.mark.asyncio
    async def test_prepare_writes_trampoline(self):
        puppet, _, ap = self._make_puppet()

        await puppet.prepare(0x08001000, 0x42)

        # Trampoline zone should contain trampoline code + function pointer
        addr = puppet._trampoline.address
        off = addr - 0x20000000
        written = bytes(ap._memory[off:off + len(ARM_M_TRAMPOLINE) + 4])
        assert written[:len(ARM_M_TRAMPOLINE)] == ARM_M_TRAMPOLINE
        func_ptr = struct.unpack_from("<I", written, len(ARM_M_TRAMPOLINE))[0]
        assert func_ptr == 0x08001000

    @pytest.mark.asyncio
    async def test_run_resumes_cpu(self):
        puppet, cpu, _ = self._make_puppet()
        await puppet.run()
        assert cpu.resume_count == 1

    @pytest.mark.asyncio
    async def test_wait_returns_r0(self):
        puppet, cpu, _ = self._make_puppet(halt_after=1)
        cpu.return_value = 0xdeadbeef
        cpu._regs[CortexReg.R0] = 0xdeadbeef

        result = await puppet.wait()
        assert result == 0xdeadbeef

    @pytest.mark.asyncio
    async def test_wait_polls_until_halt(self):
        puppet, cpu, _ = self._make_puppet(halt_after=3)
        cpu.return_value = 42

        # CPU starts running
        cpu._state = CpuState.RUN
        cpu._resumed = True

        result = await puppet.wait(timeout=1.0)
        assert result == 42
        assert cpu._poll_count == 3

    @pytest.mark.asyncio
    async def test_wait_timeout_force_halt(self):
        puppet, cpu, _ = self._make_puppet(halt_after=999999)
        cpu._state = CpuState.RUN
        cpu._resumed = True

        result = await puppet.wait(timeout=0.01)
        assert cpu.halt_count == 1

    @pytest.mark.asyncio
    async def test_call(self):
        puppet, cpu, _ = self._make_puppet(halt_after=1)
        cpu.return_value = 0x42

        result = await puppet.call(0x08001000, 0x11, 0x22)
        assert result == 0x42
        assert cpu.resume_count == 1


class TestPuppetStub:
    @pytest.mark.asyncio
    async def test_stub_call(self):
        cpu = MockCortex(halt_after=1, return_value=99)
        ap = MockMemAp()
        puppet = Puppet(cpu, ap, 0x20000000, 0x4000, ARM_M_TRAMPOLINE)

        code = b"\x00\xbf" * 4  # nop nop nop nop
        stub = puppet.stub(code)

        result = await stub.call(0x11, 0x22)
        assert result == 99
        stub.cleanup()

    @pytest.mark.asyncio
    async def test_stub_cleanup_frees_memory(self):
        cpu = MockCortex()
        ap = MockMemAp()
        puppet = Puppet(cpu, ap, 0x20000000, 0x4000, ARM_M_TRAMPOLINE)

        code = b"\x00" * 32
        stub = puppet.stub(code)
        assert stub.zone is not None

        stub.cleanup()
        assert stub.zone is None

        # Double cleanup is safe
        stub.cleanup()

    @pytest.mark.asyncio
    async def test_stub_prepare_run_wait(self):
        cpu = MockCortex(halt_after=1, return_value=7)
        ap = MockMemAp()
        puppet = Puppet(cpu, ap, 0x20000000, 0x4000, ARM_M_TRAMPOLINE)

        code = b"\x00\xbf" * 2
        stub = puppet.stub(code)

        await stub.prepare(0xaa)
        await stub.run()
        result = await stub.wait()
        assert result == 7
        stub.cleanup()


class TestArmMPuppet:
    def test_creates_with_arm_trampoline(self):
        cpu = MockCortex()
        ap = MockMemAp()
        puppet = ArmMPuppet(cpu, ap, 0x20000000, 0x4000)
        assert puppet._trampoline_code == ARM_M_TRAMPOLINE
