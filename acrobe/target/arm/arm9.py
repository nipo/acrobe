"""ARM9E-S family run-control view.

`Arm9Core` drives one core through its `Arm9Debug` component:
halting, register access and memory access all go through
instruction stuffing on scan chain 1. Memory accesses clobber
r0-r14 and can switch the core to Abort mode, so the core context
is saved on debug entry and written back on resume.
"""

from __future__ import annotations

import asyncio

from ...component.arm.a32_state import Mode, Psr
from ...component.arm.arm9.arm926 import Arm926Cp15
from ...component.arm.arm9.debug import Arm9Debug
from ...component.arm.arm9.embedded_ice import (
    DebugControl, DebugStatus, MethodOfEntry,
)
from ..debuggable import (
    Core, CoreState, Debuggable, HaltCause, Register, RegisterType,
)


# `number` is the GDB regnum of the `org.gnu.gdb.arm.core` feature,
# where cpsr sits at 25 after the legacy FPA registers.
ARM_CORE_REGISTERS = tuple(
    Register(n, f"r{n}", 32, RegisterType.GPR, "general")
    for n in range(13)
) + (
    Register(13, "sp", 32, RegisterType.SP, "general"),
    Register(14, "lr", 32, RegisterType.LR, "general"),
    Register(15, "pc", 32, RegisterType.PC, "general"),
    Register(25, "cpsr", 32, RegisterType.SYSTEM, "general"),
)


class Arm9MemoryAbort(IOError):
    pass


class Arm9Core(Core):
    """One ARM9E-S family core."""

    gdb_feature_name = "org.gnu.gdb.arm.core"
    gdb_byteorder = "little"

    CPSR = 25

    # The first instruction after debug entry from ARM state sees
    # pc 4 addresses past the resume address, and STM stores pc
    # 3 addresses past the storing instruction.
    PC_ENTRY_OFFSET = 24

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
        self.registers = list(ARM_CORE_REGISTERS)
        self.__by_name = {r.name: r for r in self.registers}
        self.__by_number = {r.number: r for r in self.registers}
        self.__context: dict[int, int] | None = None
        self.__entry: DebugStatus | None = None

    def lookup_register(self, key) -> Register:
        if isinstance(key, Register):
            return key
        if isinstance(key, int):
            return self.__by_number[key]
        return self.__by_name[str(key)]

    async def state(self) -> CoreState:
        status = await self.debug.status()
        return CoreState.HALT if status.dbgack else CoreState.RUN

    async def halt_cause(self) -> HaltCause:
        await self.__halted()
        return self.__HALT_CAUSES.get(self.__entry.moe, HaltCause.UNKNOWN)

    async def halt(self):
        if self.__context is not None:
            return
        status = await self.debug.status()
        if not status.dbgack:
            await self.debug.control_set(DebugControl(dbgrq=True))
            await self.debug.wait_status(lambda s: s.dbgack,
                                         timeout=self.HALT_TIMEOUT)
        await self.__debug_entry()

    async def __debug_entry(self):
        """Save the context. Must run before any other instruction
        goes through the pipeline, as the pc correction depends on
        it."""
        await self.debug.control_set(DebugControl(dbgack=True, intdis=True))
        status = await self.debug.status()
        if status.tbit or status.jbit:
            raise NotImplementedError("Debug entry from Thumb or Jazelle state")
        regs = await asyncio.gather(*self.debug.post_regs_read(range(16)))
        cpsr = await self.debug.post_psr_read()
        context = dict(enumerate(regs))
        context[15] -= self.PC_ENTRY_OFFSET
        context[self.CPSR] = cpsr
        self.__context = context
        self.__entry = status

    async def __halted(self) -> dict[int, int]:
        if self.__context is None:
            status = await self.debug.status()
            if not status.dbgack:
                raise RuntimeError(f"{self.name} is running")
            await self.__debug_entry()
        return self.__context

    async def resume(self, *, allow_interrupts=True):
        if not allow_interrupts:
            raise NotImplementedError("Resuming with interrupts masked")
        context = await self.__halted()
        self.debug.post_psr_write(context[self.CPSR])
        self.debug.post_regs_write({r: context[r] for r in range(15)})
        self.debug.post_pc_write(context[15])
        await self.debug.control_set(DebugControl())
        self.__context = None
        self.__entry = None
        await self.debug.post_branch_resume()

    async def run_at(self, address: int):
        """Start executing ARM code at `address` from Supervisor mode
        with interrupts masked, as out of reset."""
        await self.halt()
        cpsr = Psr(mode=Mode.SVC, I=True, F=True)
        self.__context[self.CPSR] = int(cpsr)
        self.__context[15] = address
        await self.resume()

    async def step(self):
        raise NotImplementedError("Single step")

    async def reset(self, *, stop=True):
        raise NotImplementedError("ARM9 core reset")

    async def reg_read(self, regs):
        context = await self.__halted()
        registers = [self.lookup_register(r) for r in regs]
        return {r: context[r.number] for r in registers}

    async def reg_write(self, reg_values):
        context = await self.__halted()
        for k, v in reg_values.items():
            context[self.lookup_register(k).number] = int(v) & 0xffffffff

    # Memory

    @staticmethod
    def access_plan(address: int, size: int) -> list[tuple[int, int]]:
        """Split a span into (width, count) runs of naturally aligned
        accesses, each run at most 14 units long."""
        units = []
        end = address + size
        while address < end:
            width = 4
            while address % width or address + width > end:
                width //= 2
            units.append(width)
            address += width
        runs = []
        for width in units:
            if runs and runs[-1][0] == width and runs[-1][1] < 14:
                runs[-1] = (width, runs[-1][1] + 1)
            else:
                runs.append((width, 1))
        return runs

    async def __memory_access(self, address: int, size: int, transfer):
        """Run `transfer(width, count, offset)` for each run of the
        span with r0 pointing at it, then check for an abort."""
        context = await self.__halted()
        self.debug.post_regs_write({0: address})
        values = []
        offset = 0
        for width, count in self.access_plan(address, size):
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
        return b"".join((v & ((1 << (8 * w)) - 1)).to_bytes(w, "little")
                        for w, v in zip(widths, values))

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
