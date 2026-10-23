"""Run-control shared by A/R-profile and classic ARM cores.

Those cores are driven by executing instructions in Debug state,
which clobbers core registers. The core context is saved once on
debug entry, register accesses are served from the saved copy, and
the copy is written back on resume.
"""

from __future__ import annotations

from ...component.arm.a32_state import Mode, Psr
from ..debuggable import Core, CoreState, HaltCause, Register, RegisterType


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


class ArmCore(Core):
    """Subclasses provide the debug-logic hooks `debug_halted`,
    `debug_halt`, `debug_restart`, `context_save` and
    `context_restore`."""

    gdb_feature_name = "org.gnu.gdb.arm.core"
    gdb_byteorder = "little"

    PC = 15
    CPSR = 25

    def __init__(self, name: str, registers=ARM_CORE_REGISTERS):
        super().__init__(name)
        self.registers = list(registers)
        self.__by_name = {r.name: r for r in self.registers}
        self.__by_number = {r.number: r for r in self.registers}
        self.__context: dict[int, int] | None = None
        self.__cause: HaltCause | None = None

    def lookup_register(self, key) -> Register:
        if isinstance(key, Register):
            return key
        if isinstance(key, int):
            return self.__by_number[key]
        return self.__by_name[str(key)]

    # Debug-logic hooks

    async def debug_halted(self) -> bool:
        raise NotImplementedError()

    async def debug_halt(self):
        raise NotImplementedError()

    async def debug_restart(self):
        raise NotImplementedError()

    async def context_save(self) -> tuple[dict[int, int], HaltCause]:
        """Read r0-r15 and cpsr, keyed by register number, pc being
        the address to resume at. Runs first after debug entry."""
        raise NotImplementedError()

    async def context_restore(self, context: dict[int, int]):
        raise NotImplementedError()

    # Core API

    async def state(self) -> CoreState:
        return CoreState.HALT if await self.debug_halted() else CoreState.RUN

    async def halted_context(self) -> dict[int, int]:
        if self.__context is None:
            if not await self.debug_halted():
                raise RuntimeError(f"{self.name} is running")
            self.__context, self.__cause = await self.context_save()
        return self.__context

    async def halt_cause(self) -> HaltCause:
        await self.halted_context()
        return self.__cause

    async def halt(self):
        if self.__context is not None:
            return
        if not await self.debug_halted():
            await self.debug_halt()
        await self.halted_context()

    async def resume(self, *, allow_interrupts=True):
        await self.resume_prepare(allow_interrupts=allow_interrupts)
        await self.debug_restart()

    async def resume_prepare(self, *, allow_interrupts=True):
        """Everything resume does but the restart request, for
        restarting several cores at once."""
        if not allow_interrupts:
            raise NotImplementedError("Resuming with interrupts masked")
        context = await self.halted_context()
        await self.context_restore(context)
        self.__context = None
        self.__cause = None

    async def run_at(self, address: int):
        """Start executing ARM code at `address` from Supervisor mode
        with interrupts masked, as out of reset."""
        await self.halt()
        context = await self.halted_context()
        context[self.CPSR] = int(Psr(mode=Mode.SVC, I=True, F=True))
        context[self.PC] = address
        await self.resume()

    async def reg_read(self, regs):
        context = await self.halted_context()
        registers = [self.lookup_register(r) for r in regs]
        return {r: context[r.number] for r in registers}

    async def reg_write(self, reg_values):
        context = await self.halted_context()
        for k, v in reg_values.items():
            context[self.lookup_register(k).number] = int(v) & 0xffffffff

    @staticmethod
    def access_plan(address: int, size: int, run: int) -> list[tuple[int, int]]:
        """Split a span into (width, count) runs of naturally aligned
        accesses, each run at most `run` units long."""
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
            if runs and runs[-1][0] == width and runs[-1][1] < run:
                runs[-1] = (width, runs[-1][1] + 1)
            else:
                runs.append((width, 1))
        return runs

    @staticmethod
    def units_to_bytes(widths, values) -> bytes:
        return b"".join((v & ((1 << (8 * w)) - 1)).to_bytes(w, "little")
                        for w, v in zip(widths, values))
