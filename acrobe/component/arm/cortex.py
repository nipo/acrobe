"""Cortex-M debug through System Control Space (SCS).

Provides halt/resume/step/reset and CPU register access via the
Debug Halting Control/Status Register (DHCSR) and Debug Core
Register Selector/Data Registers (DCRSR/DCRDR).
"""

from __future__ import annotations

import asyncio
from enum import IntEnum

from ...node import Node
from .ap import MemAp


# --- CPU state ---

class CpuState(IntEnum):
    UNKNOWN = 0
    RUN = 1
    HALT = 2
    LOCKUP = 3
    SLEEP = 4


class HaltCause(IntEnum):
    NONE = 0
    DEBUGGER = 1
    BREAKPOINT = 2
    WATCHPOINT = 3
    VECTOR_CATCH = 4
    EXTERNAL = 5


# --- SCS register offsets (from SCS base, typically 0xE000E000) ---

class ScsRegs:
    CPUID = 0xd00
    ICSR = 0xd04
    VTOR = 0xd08
    AIRCR = 0xd0c
    DFSR = 0xd30
    DHCSR = 0xdf0
    DCRSR = 0xdf4
    DCRDR = 0xdf8
    DEMCR = 0xdfc


# --- DHCSR bits ---

DHCSR_KEY = 0xa05f0000
C_DEBUGEN = 1 << 0
C_HALT = 1 << 1
C_STEP = 1 << 2
C_MASKINTS = 1 << 3
S_REGRDY = 1 << 16
S_HALT = 1 << 17
S_SLEEP = 1 << 18
S_LOCKUP = 1 << 19
S_RETIRE_ST = 1 << 24
S_RESET_ST = 1 << 25

# --- DFSR bits ---

DFSR_HALTED = 1 << 0
DFSR_BKPT = 1 << 1
DFSR_DWTTRAP = 1 << 2
DFSR_VCATCH = 1 << 3
DFSR_EXTERNAL = 1 << 4

# --- AIRCR ---

AIRCR_KEY = 0x05fa0000
AIRCR_SYSRESETREQ = 1 << 2

# --- DEMCR ---

DEMCR_VC_CORERESET = 1 << 0
DEMCR_TRCENA = 1 << 24

# --- CPU registers ---

class CortexReg(IntEnum):
    R0 = 0
    R1 = 1
    R2 = 2
    R3 = 3
    R4 = 4
    R5 = 5
    R6 = 6
    R7 = 7
    R8 = 8
    R9 = 9
    R10 = 10
    R11 = 11
    R12 = 12
    SP = 13
    LR = 14
    PC = 15
    XPSR = 16
    MSP = 17
    PSP = 18
    CFBP = 20


class Cortex(Node):
    """Cortex-M debug interface.

    Reads/writes CPU state through the SCS registers in the MEM-AP.
    """

    def __init__(self, mem_ap: MemAp, scs_base: int = 0xe000e000,
                 name: str = "cortex"):
        super().__init__(name)
        self._ap = mem_ap
        self._base = scs_base

    def _addr(self, reg_offset: int) -> int:
        return self._base + reg_offset

    async def _read_reg(self, offset: int) -> int:
        return await self._ap.read32(self._addr(offset))

    async def _write_reg(self, offset: int, data: int):
        await self._ap.write32(self._addr(offset), data)

    async def _dhcsr_write(self, value: int):
        """Write DHCSR with key."""
        await self._write_reg(ScsRegs.DHCSR, DHCSR_KEY | (value & 0xffff))

    async def _dhcsr_read(self) -> int:
        return await self._read_reg(ScsRegs.DHCSR)

    # --- State queries ---

    async def state(self) -> CpuState:
        """Read current CPU state."""
        dhcsr = await self._dhcsr_read()
        if dhcsr & S_LOCKUP:
            return CpuState.LOCKUP
        if dhcsr & S_SLEEP:
            return CpuState.SLEEP
        if dhcsr & S_HALT:
            return CpuState.HALT
        return CpuState.RUN

    async def halt_cause(self) -> HaltCause:
        """Read the cause of the last halt."""
        dfsr = await self._read_reg(ScsRegs.DFSR)
        if dfsr & DFSR_EXTERNAL:
            return HaltCause.EXTERNAL
        if dfsr & DFSR_DWTTRAP:
            return HaltCause.WATCHPOINT
        if dfsr & (DFSR_BKPT | DFSR_VCATCH):
            return HaltCause.BREAKPOINT
        if dfsr & DFSR_HALTED:
            return HaltCause.DEBUGGER
        return HaltCause.NONE

    # --- CPU control ---

    async def halt(self):
        """Halt the CPU."""
        await self._dhcsr_write(C_DEBUGEN | C_HALT)

        # Verify halted
        for _ in range(100):
            if await self.state() == CpuState.HALT:
                return
        raise TimeoutError("CPU did not halt")

    async def resume(self):
        """Resume CPU execution."""
        # Clear debug fault status
        await self._write_reg(ScsRegs.DFSR, 0x1f)
        await self._dhcsr_write(C_DEBUGEN)

    async def step(self):
        """Single-step the CPU."""
        await self._write_reg(ScsRegs.DFSR, 0x1f)
        await self._dhcsr_write(C_DEBUGEN | C_HALT)
        await self._dhcsr_write(C_DEBUGEN | C_STEP)

        # Wait for halt after step
        for _ in range(100):
            if await self.state() == CpuState.HALT:
                return
        raise TimeoutError("CPU did not halt after step")

    async def reset(self, halt_after: bool = True):
        """Reset the CPU.

        If halt_after is True, sets vector catch on reset to halt
        immediately after reset.
        """
        if halt_after:
            demcr = await self._read_reg(ScsRegs.DEMCR)
            await self._write_reg(ScsRegs.DEMCR, demcr | DEMCR_VC_CORERESET)

        await self._write_reg(ScsRegs.AIRCR,
                              AIRCR_KEY | AIRCR_SYSRESETREQ)

        # Wait for reset to complete
        for _ in range(100):
            try:
                dhcsr = await self._dhcsr_read()
                if not (dhcsr & S_RESET_ST):
                    break
            except Exception:
                # DP may be temporarily unreachable during reset
                await asyncio.sleep(0.01)

        if halt_after:
            for _ in range(100):
                if await self.state() == CpuState.HALT:
                    # Clear vector catch
                    demcr = await self._read_reg(ScsRegs.DEMCR)
                    await self._write_reg(ScsRegs.DEMCR,
                                          demcr & ~DEMCR_VC_CORERESET)
                    return
            raise TimeoutError("CPU did not halt after reset")

    # --- Register access ---

    async def reg_read(self, reg: int) -> int:
        """Read a single CPU register."""
        await self._write_reg(ScsRegs.DCRSR, reg)
        # Wait for S_REGRDY
        for _ in range(100):
            dhcsr = await self._dhcsr_read()
            if dhcsr & S_REGRDY:
                return await self._read_reg(ScsRegs.DCRDR)
        raise TimeoutError("Register read timeout")

    async def reg_write(self, reg: int, value: int):
        """Write a single CPU register."""
        await self._write_reg(ScsRegs.DCRDR, value)
        await self._write_reg(ScsRegs.DCRSR, reg | 0x10000)
        for _ in range(100):
            dhcsr = await self._dhcsr_read()
            if dhcsr & S_REGRDY:
                return
        raise TimeoutError("Register write timeout")

    async def regs_read(self, regs: list[int]) -> dict[int, int]:
        """Read multiple CPU registers. Returns {reg: value}."""
        result = {}
        for reg in regs:
            result[reg] = await self.reg_read(reg)
        return result

    async def regs_write(self, reg_values: dict[int, int]):
        """Write multiple CPU registers."""
        for reg, value in reg_values.items():
            await self.reg_write(reg, value)

    async def read_cpuid(self) -> int:
        """Read the CPUID register."""
        return await self._read_reg(ScsRegs.CPUID)

    # --- Memory access (convenience, delegates to MEM-AP) ---

    async def mem_read32(self, addr: int) -> int:
        """Read a 32-bit word from memory."""
        return await self._ap.read32(addr)

    async def mem_write32(self, addr: int, data: int):
        """Write a 32-bit word to memory."""
        await self._ap.write32(addr, data)

    async def mem_read_block(self, addr: int, word_count: int) -> list[int]:
        """Read a block of 32-bit words from memory."""
        return await self._ap.read_block(addr, word_count)

    def __repr__(self):
        return f"<Cortex {self._name} base={self._base:#010x}>"
