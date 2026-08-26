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

import asyncio

from ...component.arm.coresight.dwt import Dwt
from ...component.arm.coresight.fpb import Fpb
from ...component.arm.coresight.scs import Scs
from ..debuggable import (
    Core, CoreState, Debuggable, HaltCause, Register, RegisterType,
)
from ..region import Ram
from ..target import Target


# Cortex-M registers — `number` is the **GDB regnum** as expected by
# stock GDB's `org.gnu.gdb.arm.m-profile` / `m-system` features.
# These match what GDB's internal ARM unwinder hard-codes (notably
# xpsr at 25); a `g` reply that disagrees on regnum makes GDB
# misalign on unwind.
#
# The chip-side DCRSR selector — completely different numbering —
# lives in `CortexMCore.__DCRSR_SELECTOR` and is consulted only
# inside `reg_read` / `reg_write`.
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
    Register(25, "xpsr", 32, RegisterType.SYSTEM, "general"),
    Register(26, "msp",  32, RegisterType.SP,     "system"),
    Register(27, "psp",  32, RegisterType.SP,     "system"),
    Register(28, "cfbp", 32, RegisterType.SYSTEM, "system"),
)


class CortexMCore(Core):
    """One Cortex-M execution thread (one physical CPU)."""

    gdb_feature_name = "org.gnu.gdb.arm.m-profile"
    gdb_byteorder = "little"

    # GDB regnum (in CORTEX_M_REGISTERS) → DCRSR selector. The two
    # number spaces coincide for r0..pc, then diverge: stock GDB
    # has xpsr at regnum 25 (its internal unwinder hard-codes it),
    # while DCRSR carries xpsr at selector 16.
    __DCRSR_SELECTOR = {
        "r0": 0, "r1": 1, "r2": 2, "r3": 3, "r4": 4, "r5": 5,
        "r6": 6, "r7": 7, "r8": 8, "r9": 9, "r10": 10, "r11": 11,
        "r12": 12,
        "sp": 13, "lr": 14, "pc": 15,
        "xpsr": 16,
        "msp": 17, "psp": 18,
        "cfbp": 20,
    }

    def __init__(self, name: str, scs: Scs, fpb: Fpb | None = None,
                 dwt: Dwt | None = None):
        super().__init__(name)
        self.scs = scs
        self.fpb = fpb
        self.dwt = dwt
        self.registers = list(CORTEX_M_REGISTERS)
        self.__by_name = {r.name: r for r in self.registers}
        self.__by_number = {r.number: r for r in self.registers}
        # Per-comparator GDB kind. FPB itself doesn't remember kinds,
        # so the Core tracks them to reconstruct Z-packet tuples.
        self.__bp_kinds: dict[int, int] = {}
        # DWT watchpoint bookkeeping: index -> (addr, size, kind).
        self.__wp_state: dict[int, tuple[int, int, int]] = {}

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

    # Timeouts for the post-op state-settle waits. Resume's window
    # is microseconds in practice (one instruction); halt and step
    # can be longer if the core is in WFI / WFE or a stalled
    # memory transaction. These are upper bounds before we give up
    # and return — the caller can still observe the actual state.
    RESUME_SETTLE = 0.05
    HALT_SETTLE = 0.5
    STEP_SETTLE = 0.5

    async def halt(self) -> None:
        await self.scs.cpu_halt()
        await self.__settle(want_halt=True, timeout=self.HALT_SETTLE)

    async def resume(self, *, allow_interrupts: bool = True) -> None:
        await self.scs.cpu_resume(allow_interrupts=allow_interrupts)
        await self.__settle(want_halt=False, timeout=self.RESUME_SETTLE)

    async def step(self) -> None:
        await self.scs.cpu_step()
        await self.__settle(want_halt=True, timeout=self.STEP_SETTLE)

    async def __settle(self, *, want_halt: bool, timeout: float):
        """Poll DHCSR until S_HALT matches `want_halt` or the
        timeout elapses.

        Per ARMv7-M ARM §C1.6.3, S_HALT is UNKNOWN immediately
        after clearing C_HALT (resume) — until the first
        instruction retires. A snapshot taken right after the
        DHCSR write can still read 1. Same story in reverse for
        halt / step: the bit may briefly read 0 before the core
        finishes whatever it was mid-doing.

        We poll at 1 ms granularity so the round-trip cost is
        bounded; the typical settle is < 100 µs."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            dhcsr = await self.scs.read_dhcsr()
            halted = bool(dhcsr & Scs.DHCSR_S_HALT)
            if halted == want_halt:
                return
            if loop.time() > deadline:
                return
            await asyncio.sleep(0.001)

    async def reset(self, *, stop: bool = True) -> None:
        """SYSRESETREQ. With `stop=True` set DEMCR.VC_CORERESET first
        so the core comes up halted; restore it after.

        Emits `(reset, pre/post)` with `kind="cpu"` so subscribers
        (auto-reload consoles, RTT rebind, logging) can react."""
        async with self.event_emitter("reset", kind="cpu", stop=stop):
            if stop:
                await self.scs.set_reset_catch(True)
            try:
                await self.scs.cpu_reset()
            finally:
                if stop:
                    await self.scs.set_reset_catch(False)

    async def reg_read(self, regs):
        registers = [self.lookup_register(r) for r in regs]
        selectors = [self.__DCRSR_SELECTOR[r.name] for r in registers]
        values = await self.scs.cpu_regs_get(selectors)
        return dict(zip(registers, values))

    async def reg_write(self, reg_values):
        pairs = []
        for k, v in reg_values.items():
            r = self.lookup_register(k)
            pairs.append((self.__DCRSR_SELECTOR[r.name], v))
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

    # -- DWT watchpoints -------------------------------------------

    # GDB Z-packet types → DWT FUNCTION values.
    __DWT_FUNC = {
        2: Dwt.FUNC_DATA_WRITE,   # Z2 — write watchpoint
        3: Dwt.FUNC_DATA_READ,    # Z3 — read watchpoint
        4: Dwt.FUNC_DATA_ACCESS,  # Z4 — access watchpoint
    }

    async def watchpoint_add(self, addr, size, kind):
        """Add a data-address watchpoint.

        `kind` is the GDB Z-packet type number (2/3/4 for write /
        read / access). `size` is the watched span in bytes; the
        DWT MASK field encodes log2(size), so the size must be a
        power of two between 1 and 32768.

        Returns a tuple usable to identify the watchpoint later in
        `watchpoint_remove`; matches the GDB Z-tuple shape (type,
        addr, kind=size)."""
        if self.dwt is None:
            raise NotImplementedError("Core has no DWT attached")
        if self.dwt.comparator_count == 0:
            raise RuntimeError("DWT has no comparators")
        try:
            function = self.__DWT_FUNC[kind]
        except KeyError:
            raise ValueError(f"unknown watchpoint kind {kind}") from None
        index = self.dwt.allocate()
        if index is None:
            raise RuntimeError("No free DWT comparator")
        await self.dwt.comp_set(
            index, addr=addr, size=size, function=function)
        self.__wp_state[index] = (addr, size, kind)
        return (kind, addr, size)

    async def watchpoint_remove(self, wp):
        if self.dwt is None:
            raise NotImplementedError("Core has no DWT attached")
        kind, addr, size = wp
        for index, (a, s, k) in list(self.__wp_state.items()):
            if (a, s, k) == (addr, size, kind):
                await self.dwt.comp_clear(index)
                self.__wp_state.pop(index, None)
                return
        raise KeyError(f"watchpoint {wp!r} not found")

    async def watchpoint_list(self):
        if self.dwt is None:
            return []
        return [(kind, addr, size)
                for (addr, size, kind) in self.__wp_state.values()]

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
        # ARM-defined private peripheral bus (PPB) — SCS, DWT, FPB,
        # ITM, TPIU, ETM, vendor extensions. Always at 0xE0000000,
        # 1 MiB span. Declared so GDB's memory-map clamping doesn't
        # block `x/...` of debug peripherals.
        self.memory_map.append(Ram("ppb", 0xE0000000, 0x100000))

    @classmethod
    def from_romtable(cls, rom_table, mem_ap, *, name: str = "debug"):
        """Build a Debuggable from one ROM Table.

        Picks every SCS under the ROM Table — multi-core SoCs route
        each CPU's SCS through a sibling ROM Table — and pairs each
        with its sibling FPB and DWT (if any) to construct one
        CortexMCore per SCS."""
        debuggable = cls(mem_ap, name=name)
        scs_list = rom_table.children_of_class(Scs)
        fpb_list = rom_table.children_of_class(Fpb)
        dwt_list = rom_table.children_of_class(Dwt)
        fpb = fpb_list[0] if len(fpb_list) == 1 else None
        dwt = dwt_list[0] if len(dwt_list) == 1 else None
        for index, scs in enumerate(scs_list):
            core_name = f"core{index}" if len(scs_list) > 1 else "core"
            debuggable.child_add(
                CortexMCore(core_name, scs, fpb=fpb, dwt=dwt))
        return debuggable

    async def attach(self) -> None:
        """Enable debug, halt every core, and turn on the hardware
        breakpoint unit. Run in that order: halt needs DEBUGEN
        already set; FPB enables don't take effect on a running
        core.

        Halt-on-attach is the convention every GDB front-end
        expects — `?` and `g` are sent immediately after the
        connection handshake, and DCRSR / DCRDR register transfers
        only return valid data on a halted core. Without halting,
        the first `info reg` shows zeros and GDB then trips itself
        up trying to unwind from PC=0.
        """
        await super().attach()
        for core in self.cores:
            await core.scs.enable_debug()
        for core in self.cores:
            await core.halt()
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

    async def monitor(self, cmd: str, args: list[str]) -> str:
        """GDB `monitor <cmd>` dispatch.

        Standard Cortex-M verbs (reset / halt / resume / erase /
        help). Returns the text echoed back to the GDB user via the
        qRcmd reply. Raise `NotImplementedError` for unknown commands
        — the Responder turns that into "Unknown monitor command:
        <name>" so chip subclasses can override only the additions
        they want.
        """
        if cmd == "help":
            return self.__help_text()
        if cmd == "reset":
            mode = args[0] if args else "halt"
            if mode in ("halt", "stop"):
                for core in self.cores:
                    await core.reset(stop=True)
                return "Reset (halted at vector).\n"
            if mode in ("run", "go"):
                for core in self.cores:
                    await core.reset(stop=False)
                return "Reset (running).\n"
            return (f"Unknown reset mode {mode!r}. "
                    f"Try `monitor reset halt` or `monitor reset run`.\n")
        if cmd == "halt":
            for core in self.cores:
                await core.halt()
            return "Halted.\n"
        if cmd in ("resume", "continue", "cont", "go"):
            for core in self.cores:
                await core.resume()
            return "Resumed.\n"
        if cmd in ("erase", "erase-all", "erase_all"):
            return await self.__monitor_erase()
        raise NotImplementedError(cmd)

    async def __monitor_erase(self) -> str:
        """Route `monitor erase` to the sibling Loadable.erase_all.

        Works for any Cortex-M target with a Loadable — nRF52
        picks up the CTRL-AP fast path automatically since
        Nrf52Loadable.erase_all overrides the default."""
        from ..loadable import Loadable
        target = self.parent
        if target is None:
            return "no target attached\n"
        loadables = target.children_of_class(Loadable)
        if not loadables:
            return "no Loadable attached to this target\n"
        if len(loadables) > 1:
            names = ", ".join(l.name for l in loadables)
            return (f"target has multiple Loadables ({names}); "
                    f"use the CLI's `chip erase-all` with --loadable.\n")
        await loadables[0].erase_all()
        return "Erased.\n"

    @staticmethod
    def __help_text() -> str:
        return (
            "Monitor commands:\n"
            "  reset [halt|run]   Reset the target (default: halt at vector).\n"
            "  halt               Halt all cores.\n"
            "  resume             Resume all cores.\n"
            "  erase              Mass-erase via the sibling Loadable.\n"
            "  help               Show this help.\n")


class CortexMTarget(Target):
    """Target holding one CortexMDebuggable. Concrete by virtue of
    its CortexMDebuggable child; subclasses (STM32 et al., S2b)
    add Loadable + Puppet."""
