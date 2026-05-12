"""Cortex-M run-control view.

`CortexMDebuggable` (a Debuggable Node) groups one or more
`CortexMCore`s under a Target. Each Core wraps an `Scs` component
for run-control + register access and optionally an `Fpb` for
hardware breakpoints. Memory access (`mem_read`/`mem_write`) is
delegated to a `MemAp`.

The Debuggable does not own the SCS / FPB / MemAp components —
they live in the component tree. Cross-tree references are plain
attributes.
"""

from __future__ import annotations

from ...component.arm.coresight.fpb import Fpb
from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.dp import Dp
from ...component.arm.mem_ap import MemAp
from ...db import NoMatch
from ..debuggable import (
    Core, CoreState, Debuggable, HaltCause, Register, RegisterType,
)
from ..target import Target


# Cortex-M DCRSR register selectors. Numbers match the ARMv7-M /
# ARMv8-M ARM definitions.
CORTEX_M_REGISTERS = (
    Register(0,  "r0",   32, RegisterType.GPR,    "general"),
    Register(1,  "r1",   32, RegisterType.GPR,    "general"),
    Register(2,  "r2",   32, RegisterType.GPR,    "general"),
    Register(3,  "r3",   32, RegisterType.GPR,    "general"),
    Register(4,  "r4",   32, RegisterType.GPR,    "general"),
    Register(5,  "r5",   32, RegisterType.GPR,    "general"),
    Register(6,  "r6",   32, RegisterType.GPR,    "general"),
    Register(7,  "r7",   32, RegisterType.GPR,    "general"),
    Register(8,  "r8",   32, RegisterType.GPR,    "general"),
    Register(9,  "r9",   32, RegisterType.GPR,    "general"),
    Register(10, "r10",  32, RegisterType.GPR,    "general"),
    Register(11, "r11",  32, RegisterType.GPR,    "general"),
    Register(12, "r12",  32, RegisterType.GPR,    "general"),
    Register(13, "sp",   32, RegisterType.SP,     "general"),
    Register(14, "lr",   32, RegisterType.LR,     "general"),
    Register(15, "pc",   32, RegisterType.PC,     "general"),
    Register(16, "xpsr", 32, RegisterType.SYSTEM, "general"),
    Register(17, "msp",  32, RegisterType.SP,     "system"),
    Register(18, "psp",  32, RegisterType.SP,     "system"),
    Register(20, "cfbp", 32, RegisterType.SYSTEM, "system"),
)


class CortexMCore(Core):
    """One Cortex-M execution thread (one physical CPU)."""

    gdb_feature_name = "org.gnu.gdb.arm.m-profile"
    gdb_byteorder = "little"

    def __init__(self, name: str, scs: Scs, fpb: Fpb | None = None):
        super().__init__(name)
        self.scs = scs
        self.fpb = fpb
        self.registers = list(CORTEX_M_REGISTERS)
        self.__by_name = {r.name: r for r in self.registers}
        self.__by_number = {r.number: r for r in self.registers}
        # Per-comparator GDB kind. FPB itself doesn't remember kinds,
        # so the Core tracks them to reconstruct Z-packet tuples.
        self.__bp_kinds: dict[int, int] = {}

    async def dump_cpu(self, *, verbose: bool = False) -> list[str]:
        """Delegate to the underlying SCS's CPUID + feature dump."""
        return await self.scs.dump_cpu(verbose=verbose)

    def lookup_register(self, key) -> Register:
        """Resolve a Register from a Register / number / name."""
        if isinstance(key, Register):
            return key
        if isinstance(key, int):
            return self.__by_number[key]
        return self.__by_name[str(key)]

    async def state(self) -> CoreState:
        dhcsr = await self.scs.read_dhcsr()
        return self.__decode_state(dhcsr)

    async def halt_cause(self) -> HaltCause:
        dfsr = await self.scs.read_dfsr()
        return self.__decode_halt_cause(dfsr)

    async def halt(self) -> None:
        await self.scs.cpu_halt()

    async def resume(self, *, allow_interrupts: bool = True) -> None:
        await self.scs.cpu_resume(allow_interrupts=allow_interrupts)

    async def step(self) -> None:
        await self.scs.cpu_step()

    async def reset(self, *, stop: bool = True) -> None:
        """SYSRESETREQ. With `stop=True` set DEMCR.VC_CORERESET first
        so the core comes up halted; restore it after."""
        if stop:
            await self.scs.set_reset_catch(True)
        try:
            await self.scs.cpu_reset()
        finally:
            if stop:
                await self.scs.set_reset_catch(False)

    async def reg_read(self, regs):
        registers = [self.lookup_register(r) for r in regs]
        numbers = [r.number for r in registers]
        values = await self.scs.cpu_regs_get(numbers)
        return dict(zip(registers, values))

    async def reg_write(self, reg_values):
        pairs = []
        for k, v in reg_values.items():
            r = self.lookup_register(k)
            pairs.append((r.number, v))
        await self.scs.cpu_regs_set(pairs)

    async def breakpoint_add(self, addr, kind):
        """`kind` is the GDB Z-packet kind (2 = Thumb 16-bit,
        3 = Thumb 32-bit, 4 = ARM). Cortex-M is Thumb-only; both
        kinds are honoured at the FPB level."""
        if self.fpb is None:
            raise NotImplementedError("Core has no FPB attached")
        if self.fpb.code_count == 0:
            raise RuntimeError("FPB has no code comparators")
        index = self.fpb.allocate()
        if index is None:
            raise RuntimeError("No free FPB comparator")
        await self.fpb.comp_set(index, addr)
        self.__bp_kinds[index] = kind
        return (index, addr, kind)

    async def breakpoint_remove(self, bp):
        if self.fpb is None:
            raise NotImplementedError("Core has no FPB attached")
        index, _addr, _kind = bp
        await self.fpb.comp_set(index, None)
        self.__bp_kinds.pop(index, None)

    async def breakpoint_list(self):
        if self.fpb is None:
            return []
        return [(i, addr, self.__bp_kinds.get(i))
                for i, addr in self.fpb.allocations.items()
                if isinstance(addr, int) and addr >= 0]

    # -- Helpers ---------------------------------------------------

    @staticmethod
    def __decode_state(dhcsr: int) -> CoreState:
        if dhcsr & Scs.DHCSR_S_LOCKUP:
            return CoreState.LOCKUP
        if dhcsr & Scs.DHCSR_S_SLEEP:
            return CoreState.SLEEP
        if dhcsr & Scs.DHCSR_S_HALT:
            return CoreState.HALT
        return CoreState.RUN

    @staticmethod
    def __decode_halt_cause(dfsr: int) -> HaltCause:
        if dfsr & Scs.DFSR_HALTED:
            return HaltCause.DEBUGGER
        if dfsr & (Scs.DFSR_BKPT | Scs.DFSR_VCATCH):
            return HaltCause.BREAKPOINT
        if dfsr & Scs.DFSR_DWTTRAP:
            return HaltCause.WATCHPOINT
        return HaltCause.UNKNOWN


class CortexMDebuggable(Debuggable):
    """Cortex-M run-control view. Holds a MemAp for memory access
    and one or more CortexMCore children."""

    def __init__(self, mem_ap, name: str = "debug"):
        super().__init__(name)
        self.mem_ap = mem_ap

    @classmethod
    def from_romtable(cls, rom_table, mem_ap, *, name: str = "debug"):
        """Build a Debuggable from one ROM Table.

        Picks every SCS under the ROM Table — multi-core SoCs route
        each CPU's SCS through a sibling ROM Table — and pairs each
        with its sibling FPB (if any) to construct one CortexMCore."""
        debuggable = cls(mem_ap, name=name)
        scs_list = rom_table.children_of_class(Scs)
        fpb_list = rom_table.children_of_class(Fpb)
        fpb = fpb_list[0] if len(fpb_list) == 1 else None
        for index, scs in enumerate(scs_list):
            core_name = f"core{index}" if len(scs_list) > 1 else "core"
            debuggable.child_add(CortexMCore(core_name, scs, fpb=fpb))
        return debuggable

    async def attach(self) -> None:
        """Enable debug on every Core's SCS."""
        for core in self.cores:
            await core.scs.enable_debug()
        for core in self.cores:
            if core.fpb is not None:
                await core.fpb.enable(True)

    async def detach(self) -> None:
        for core in self.cores:
            if core.fpb is not None:
                await core.fpb.enable(False)
        for core in self.cores:
            await core.scs.disable_debug()

    async def mem_read(self, addr: int, size: int) -> bytes:
        return await self.mem_ap.mem_read(addr, size)

    async def mem_write(self, addr: int, data) -> None:
        await self.mem_ap.mem_write(addr, data)


@Target.register(Dp, precedence=10000)
def cortex_m_generic_target(dp):
    """Generic Cortex-M debug-only target.

    Walks the DP's MemAp+RomTable subtree looking for any RomTable
    containing an SCS. Yields a Target with one CortexMDebuggable
    child (run-control only — no Loadable; flash programming needs
    chip-specific knowledge from S2b). High precedence so chip-
    specific Targets override on declaration.
    """
    for ap in dp.children_of_class(MemAp):
        for rt in ap.children_of_class(RomTable):
            if not rt.children_of_class(Scs):
                continue
            t = CortexMTarget(f"cortex-m@{dp.name}")
            t.claim(dp, ap, rt)
            t.child_add(CortexMDebuggable.from_romtable(rt, ap))
            return t
    raise NoMatch("cortex_m_generic_target", "no SCS under DP")


class CortexMTarget(Target):
    """Target holding one CortexMDebuggable. Concrete by virtue of
    its CortexMDebuggable child; subclasses (STM32 et al., S2b)
    add Loadable + Puppet."""
