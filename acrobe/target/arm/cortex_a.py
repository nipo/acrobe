"""ARMv7-A run-control view over a v7 Debug component."""

from __future__ import annotations

from ...component.arm.a32 import A32
from ...component.arm.a32_state import Psr
from ...component.arm.coresight.v7_debug import (
    MethodOfEntry, V7Debug, V7DebugAbort,
)
from ..debuggable import HaltCause
from .arm_core import ArmCore


class CortexAMemoryAbort(IOError):
    pass


class CortexACore(ArmCore):
    """One ARMv7-A core. Memory accesses go through the core, so they
    see its current translation and cache state."""

    # Bounds the time between two abort checks.
    MEMORY_RUN = 256

    CACHE_LINE = 32

    __HALT_CAUSES = {
        MethodOfEntry.HALT_REQUEST: HaltCause.DEBUGGER,
        MethodOfEntry.EXTERNAL: HaltCause.DEBUGGER,
        MethodOfEntry.BREAKPOINT: HaltCause.BREAKPOINT,
        MethodOfEntry.BKPT: HaltCause.INSTRUCTION,
        MethodOfEntry.VECTOR_CATCH: HaltCause.EXCEPTION,
        MethodOfEntry.ASYNC_WATCHPOINT: HaltCause.WATCHPOINT,
        MethodOfEntry.SYNC_WATCHPOINT: HaltCause.WATCHPOINT,
    }

    def __init__(self, name: str, debug: V7Debug):
        super().__init__(name)
        self.debug = debug

    async def debug_halted(self) -> bool:
        return (await self.debug.dscr()).halted

    async def debug_halt(self):
        await self.debug.halt()

    async def debug_restart(self):
        await self.debug.restart()

    async def context_save(self):
        dscr = await self.debug.enter()
        context = {r: await self.debug.reg_get(r) for r in range(15)}
        # Neither read may follow a CPSR or PC update, which would
        # make the PC read UNKNOWN.
        cpsr = await self.debug.execute_get(A32.mrs(0))
        pc = await self.debug.execute_get(A32.mov(0, A32.PC))
        psr = Psr(cpsr)
        if psr.jazelle:
            raise NotImplementedError("Debug entry from Jazelle state")
        context[self.PC] = (pc - (4 if psr.thumb else 8)) & 0xffffffff
        context[self.CPSR] = cpsr
        await self.debug.reg_set(0, context[0])
        return context, self.__HALT_CAUSES.get(dscr.moe, HaltCause.UNKNOWN)

    async def context_restore(self, context):
        cpsr = context[self.CPSR]
        await self.debug.set_execute(A32.msr(0), cpsr)
        # A PC write in Thumb state needs bit 0 set.
        pc = context[self.PC] | int(Psr(cpsr).thumb)
        await self.debug.set_execute(A32.mov(A32.PC, 0), pc)
        for r in range(15):
            await self.debug.reg_set(r, context[r])

    async def step(self):
        raise NotImplementedError("Single step")

    async def reset(self, *, stop=True):
        raise NotImplementedError("Cortex-A core reset")

    # Memory

    async def __memory_access(self, address: int, size: int, transfer):
        """Run `transfer(address, width, count, offset)` for each run
        of the span. An abort leaves the core in an undefined mode,
        put the saved one back."""
        context = await self.halted_context()
        values = []
        offset = 0
        try:
            for width, count in self.access_plan(address, size, self.MEMORY_RUN):
                values.extend(await transfer(address + offset, width, count, offset))
                offset += width * count
        except V7DebugAbort as e:
            await self.debug.set_execute(A32.msr(0), context[self.CPSR])
            raise CortexAMemoryAbort(
                f"Abort accessing {size} bytes at {address + offset:#010x}") from e
        return values

    async def mem_read(self, address: int, size: int) -> bytes:
        widths = []

        async def transfer(base, width, count, offset):
            widths.extend([width] * count)
            return await self.debug.load_units(base, width, count)

        values = await self.__memory_access(address, size, transfer)
        return self.units_to_bytes(widths, values)

    async def mem_write(self, address: int, data):
        data = bytes(data)

        async def transfer(base, width, count, offset):
            values = [int.from_bytes(data[o:o + width], "little")
                      for o in range(offset, offset + width * count, width)]
            await self.debug.store_units(base, width, values)
            return []

        await self.__memory_access(address, len(data), transfer)
        await self.__caches_sync(address, len(data))

    async def __caches_sync(self, address: int, size: int):
        """Make written data visible to instruction fetch: clean the
        data cache and invalidate the instruction cache to the point
        of unification over the span."""
        line = address & ~(self.CACHE_LINE - 1)
        await self.debug.reg_set(0, line)
        while line < address + size:
            await self.debug.execute(A32.mcr(15, 0, 0, 7, 11, 1))  # DCCMVAU
            await self.debug.execute(A32.mcr(15, 0, 0, 7, 5, 1))   # ICIMVAU
            await self.debug.execute(A32.add_imm(0, 0, self.CACHE_LINE))
            line += self.CACHE_LINE
        await self.debug.execute(A32.dsb())
        await self.debug.execute(A32.mcr(15, 0, 0, 7, 5, 6))       # BPIALL
        await self.debug.execute(A32.dsb())
        await self.debug.execute(A32.isb())
