"""ARM9E-S family run-control view.

`Arm9Core` drives one core through its `Arm9Debug` component:
halting, register access and memory access all go through
instruction stuffing on scan chain 1. Memory accesses clobber
r0-r14 and can switch the core to Abort mode, so the core context
is saved on debug entry and written back on resume.
"""

from __future__ import annotations

import asyncio

from ...component.arm.a32_state import Psr
from ...component.arm.arm9.arm926 import Arm926Cp15
from ...component.arm.arm9.debug import Arm9Debug
from ...component.arm.arm9.embedded_ice import DebugControl, MethodOfEntry
from ..debuggable import CoreState, Debuggable, HaltCause
from .arm_core import ArmCore


class Arm9MemoryAbort(IOError):
    pass


class Arm9Core(ArmCore):
    """One ARM9E-S family core."""

    # The first instruction after debug entry from ARM state sees
    # pc 4 addresses past the resume address, and STM stores pc
    # 3 addresses past the storing instruction.
    PC_ENTRY_OFFSET = 24

    # Data registers r1-r14 of one LDM/STM.
    MEMORY_RUN = 14

    HALT_TIMEOUT = 0.5

    __HALT_CAUSES = {
        MethodOfEntry.WATCHPOINT0_BREAKPOINT: HaltCause.BREAKPOINT,
        MethodOfEntry.WATCHPOINT1_BREAKPOINT: HaltCause.BREAKPOINT,
        MethodOfEntry.BKPT: HaltCause.INSTRUCTION,
        MethodOfEntry.VECTOR_CATCH: HaltCause.EXCEPTION,
        MethodOfEntry.EXTERNAL_BREAKPOINT: HaltCause.BREAKPOINT,
        MethodOfEntry.WATCHPOINT0: HaltCause.WATCHPOINT,
        MethodOfEntry.WATCHPOINT1: HaltCause.WATCHPOINT,
        MethodOfEntry.EXTERNAL_WATCHPOINT: HaltCause.WATCHPOINT,
        MethodOfEntry.INTERNAL_DBGRQ: HaltCause.DEBUGGER,
        MethodOfEntry.EXTERNAL_DBGRQ: HaltCause.DEBUGGER,
    }

    def __init__(self, name: str, debug: Arm9Debug):
        super().__init__(name)
        self.debug = debug

    async def debug_halted(self) -> bool:
        return (await self.debug.status()).dbgack

    async def debug_halt(self):
        await self.debug.control_set(DebugControl(dbgrq=True))
        await self.debug.wait_status(lambda s: s.dbgack,
                                     timeout=self.HALT_TIMEOUT)

    async def context_save(self):
        await self.debug.control_set(DebugControl(dbgack=True, intdis=True))
        status = await self.debug.status()
        if status.tbit or status.jbit:
            raise NotImplementedError("Debug entry from Thumb or Jazelle state")
        regs = await asyncio.gather(*self.debug.post_regs_read(range(16)))
        cpsr = await self.debug.post_psr_read()
        context = dict(enumerate(regs))
        context[self.PC] -= self.PC_ENTRY_OFFSET
        context[self.CPSR] = cpsr
        return context, self.__HALT_CAUSES.get(status.moe, HaltCause.UNKNOWN)

    async def context_restore(self, context):
        self.debug.post_psr_write(context[self.CPSR])
        self.debug.post_regs_write({r: context[r] for r in range(15)})
        self.debug.post_pc_write(context[self.PC])
        await self.debug.control_set(DebugControl())

    async def debug_restart(self):
        await self.debug.post_branch_resume()

    async def step(self):
        raise NotImplementedError("Single step")

    async def reset(self, *, stop=True):
        raise NotImplementedError("ARM9 core reset")

    # Memory

    async def __memory_access(self, address: int, size: int, transfer):
        """Run `transfer(width, count, offset)` for each run of the
        span with r0 pointing at it, then check for an abort."""
        context = await self.halted_context()
        self.debug.post_regs_write({0: address})
        values = []
        offset = 0
        for width, count in self.access_plan(address, size, self.MEMORY_RUN):
            values.extend(await transfer(width, count, offset))
            offset += width * count
        cpsr = await self.debug.post_psr_read()
        if Psr(cpsr).mode != Psr(context[self.CPSR]).mode:
            await self.debug.post_psr_write(context[self.CPSR])
            raise Arm9MemoryAbort(
                f"Abort accessing {size} bytes at {address:#010x}")
        return values

    async def mem_read(self, address: int, size: int) -> bytes:
        widths = []

        async def transfer(width, count, offset):
            widths.extend([width] * count)
            if width == 4:
                return await self.debug.load_words(count)
            return await self.debug.load_units(width, count)

        values = await self.__memory_access(address, size, transfer)
        return self.units_to_bytes(widths, values)

    async def mem_write(self, address: int, data):
        data = bytes(data)

        async def transfer(width, count, offset):
            values = [int.from_bytes(data[o:o + width], "little")
                      for o in range(offset, offset + width * count, width)]
            if width == 4:
                await self.debug.store_words(values)
            else:
                await self.debug.store_units(width, values)
            return []

        await self.__memory_access(address, len(data), transfer)


class Arm926Core(Arm9Core):
    """ARM926EJ-S core: an ARM9EJ-S with a CP15 memory system
    reachable through scan chain 15."""

    def __init__(self, name: str, debug: Arm9Debug):
        super().__init__(name, debug)
        self.cp15 = Arm926Cp15(debug)

    async def caches_disable(self):
        await self.halt()
        await self.cp15.caches_disable()


class Arm9Debuggable(Debuggable):
    """Run-control view over a single ARM9 core. Memory goes
    through the core, which must be halted."""

    def __init__(self, core: Arm9Core, name: str = "debug"):
        super().__init__(name)
        self.core = core
        self.child_add(core)

    async def attach(self):
        await super().attach()
        await self.core.halt()

    async def detach(self):
        if await self.core.state() == CoreState.HALT:
            await self.core.resume()

    async def mem_read(self, addr: int, size: int) -> bytes:
        return await self.core.mem_read(addr, size)

    async def mem_write(self, addr: int, data):
        await self.core.mem_write(addr, data)
