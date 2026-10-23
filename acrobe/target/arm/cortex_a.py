"""ARMv7-A run-control view over a v7 Debug component."""

from __future__ import annotations

import asyncio
import contextlib

from ...component.arm.a32 import A32, PsrMask, VfpSysReg
from ...component.arm.a32_state import Mode, Psr
from ...component.arm.coresight.v7_debug import (
    Bcr, BreakpointType, MethodOfEntry, PrivilegeMatch, V7Debug,
    V7DebugAbort, WatchAccess, Wcr,
)
from ..debuggable import HaltCause, Register, RegisterType
from .arm_core import ARM_CORE_REGISTERS, ArmCore


class CortexAMemoryAbort(IOError):
    pass


class CortexARegisters:
    """Registers beyond the current-mode core set. They are only
    reachable by switching mode or enabling the FPU from Debug state,
    so GDB fetches them on demand. VFP numbers follow GDB's ARM
    layout, banked ones come after."""

    VFP_FEATURE = "org.gnu.gdb.arm.vfp"
    NEON_FEATURE = "org.gnu.gdb.arm.neon"
    BANKED_FEATURE = "org.acrobe.arm.banked"

    D0 = 26
    FPSCR = 58
    BANKED = 59

    # User registers are reached from System mode, which shares them
    # and stays privileged.
    MODES = (Mode.SYS, Mode.FIQ, Mode.IRQ, Mode.SVC, Mode.ABT, Mode.UND,
             Mode.MON)

    @classmethod
    def vfp(cls, d_count: int) -> list[Register]:
        return [
            Register(cls.D0 + i, f"d{i}", 64, RegisterType.DOUBLE, "float",
                     gdb_feature=cls.VFP_FEATURE, gdb_in_g_packet=False)
            for i in range(d_count)
        ] + [
            Register(cls.FPSCR, "fpscr", 32, RegisterType.SYSTEM, "float",
                     gdb_feature=cls.VFP_FEATURE, gdb_in_g_packet=False),
        ]

    @classmethod
    def banked(cls) -> list[tuple[Register, Mode, int | None]]:
        """(register, mode, core register index or None for SPSR)."""
        out = []
        number = cls.BANKED
        for mode in cls.MODES:
            suffix = "usr" if mode is Mode.SYS else mode.name.lower()
            indexes = list(range(8, 15)) if mode in (Mode.SYS, Mode.FIQ) \
                else list(mode.banked_registers())
            if mode.has_spsr:
                indexes.append(None)
            for index in indexes:
                name = f"spsr_{suffix}" if index is None else f"r{index}_{suffix}"
                out.append((Register(number, name, 32, RegisterType.SYSTEM,
                                     "system", gdb_feature=cls.BANKED_FEATURE,
                                     gdb_in_g_packet=False),
                            mode, index))
                number += 1
        return out


class CortexACore(ArmCore):
    """One ARMv7-A core. Memory accesses go through the core, so they
    see its current translation and cache state.

    `vfp_registers` is the number of VFP D registers, 0 without an
    FPU; `neon` tells GDB to show the Q view of them."""

    # Bounds the time between two abort checks.
    MEMORY_RUN = 256

    CACHE_LINE = 32

    SCTLR_M = 1 << 0
    SCTLR_C = 1 << 2
    SCTLR_Z = 1 << 11
    SCTLR_I = 1 << 12

    __HALT_CAUSES = {
        MethodOfEntry.HALT_REQUEST: HaltCause.DEBUGGER,
        MethodOfEntry.EXTERNAL: HaltCause.DEBUGGER,
        MethodOfEntry.BREAKPOINT: HaltCause.BREAKPOINT,
        MethodOfEntry.BKPT: HaltCause.INSTRUCTION,
        MethodOfEntry.VECTOR_CATCH: HaltCause.EXCEPTION,
        MethodOfEntry.ASYNC_WATCHPOINT: HaltCause.WATCHPOINT,
        MethodOfEntry.SYNC_WATCHPOINT: HaltCause.WATCHPOINT,
    }

    STEP_TIMEOUT = 0.5

    # GDB Z-packet types
    __WATCH_ACCESS = {
        2: WatchAccess.STORE,
        3: WatchAccess.LOAD,
        4: WatchAccess.ANY,
    }

    CPACR_CP10_CP11 = 0xf << 20
    FPEXC_EN = 1 << 30

    def __init__(self, name: str, debug: V7Debug, *, vfp_registers: int = 0,
                 neon: bool = False):
        if vfp_registers not in (0, 16, 32) or (neon and vfp_registers != 32):
            raise ValueError(f"{vfp_registers} VFP registers, neon={neon}")
        banked = CortexARegisters.banked()
        vfp = CortexARegisters.vfp(vfp_registers) if vfp_registers else []
        super().__init__(name, list(ARM_CORE_REGISTERS) + vfp
                         + [r for r, _, _ in banked])
        if neon:
            self.gdb_empty_features = (CortexARegisters.NEON_FEATURE,)
        self.debug = debug
        self.__vfp_count = vfp_registers
        self.__banked = {r.number: (mode, index) for r, mode, index in banked}
        # Values of the on-demand registers read or written during
        # this halt; written ones are put back on resume.
        self.__shadow: dict[int, int] = {}
        self.__dirty: set[int] = set()
        self.__breakpoints: dict[int, tuple[int, int]] = {}
        self.__watchpoints: dict[int, tuple[int, int, int]] = {}
        self.__breakpoint_count: int | None = None
        self.__watchpoint_count: int | None = None
        self.__stepping = False

    async def debug_halted(self) -> bool:
        return (await self.debug.dscr()).halted

    async def debug_halt(self):
        await self.debug.halt()

    async def debug_restart(self):
        await self.debug.restart()

    async def context_save(self):
        self.__shadow.clear()
        self.__dirty.clear()
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
        if self.__stepping:
            return context, HaltCause.DEBUGGER
        return context, self.__HALT_CAUSES.get(dscr.moe, HaltCause.UNKNOWN)

    async def context_restore(self, context):
        await self.__shadow_flush(context)
        cpsr = context[self.CPSR]
        await self.debug.set_execute(A32.msr(0), cpsr)
        # A PC write in Thumb state needs bit 0 set.
        pc = context[self.PC] | int(Psr(cpsr).thumb)
        await self.debug.set_execute(A32.mov(A32.PC, 0), pc)
        for r in range(15):
            await self.debug.reg_set(r, context[r])

    # On-demand registers

    async def reg_read(self, regs):
        registers = [self.lookup_register(r) for r in regs]
        context = await self.halted_context()
        values = await super().reg_read(
            [r for r in registers if r.number in context])
        for r in registers:
            if r.number in context:
                continue
            alias = self.__alias(context, r.number)
            if alias is not None:
                values[r] = context[alias]
                continue
            if r.number not in self.__shadow:
                await self.__fetch(context, r.number)
            values[r] = self.__shadow[r.number]
        return {r: values[r] for r in registers}

    async def reg_write(self, reg_values):
        context = await self.halted_context()
        plain = {}
        for k, v in reg_values.items():
            r = self.lookup_register(k)
            alias = None if r.number in context else self.__alias(context, r.number)
            if r.number in context or alias is not None:
                plain[alias if alias is not None else r.number] = v
            else:
                self.__shadow[r.number] = int(v) & ((1 << r.width) - 1)
                self.__dirty.add(r.number)
        await super().reg_write(plain)

    def __alias(self, context, number: int) -> int | None:
        """Context register a banked register currently is, if any."""
        if number not in self.__banked:
            return None
        mode, index = self.__banked[number]
        if index is None:
            return None
        current = Psr(context[self.CPSR]).mode
        if self.__owner(current, index) is self.__owner(mode, index):
            return index
        return None

    @staticmethod
    def __owner(mode: Mode, index: int) -> Mode:
        return mode.bank if mode.is_banked(index) else Mode.USR

    async def __fetch(self, context, number: int):
        if number in self.__banked:
            mode = self.__banked[number][0]
            async with self.__in_mode(context, mode):
                for n, (m, index) in self.__banked.items():
                    if m is mode and n not in self.__shadow:
                        self.__shadow[n] = await self.__banked_get(index)
            return
        async with self.__fpu_enabled():
            for i in range(self.__vfp_count):
                n = CortexARegisters.D0 + i
                if n in self.__shadow:
                    continue
                await self.debug.execute(A32.vmov_from_d(0, 1, i))
                low = await self.debug.reg_get(0)
                self.__shadow[n] = low | await self.debug.reg_get(1) << 32
            if CortexARegisters.FPSCR not in self.__shadow:
                self.__shadow[CortexARegisters.FPSCR] = \
                    await self.debug.execute_get(A32.vmrs(0))

    async def __banked_get(self, index: int | None) -> int:
        if index is None:
            return await self.debug.execute_get(A32.mrs(0, spsr=True))
        return await self.debug.reg_get(index)

    async def __shadow_flush(self, context):
        modes = {self.__banked[n][0] for n in self.__dirty if n in self.__banked}
        for mode in modes:
            async with self.__in_mode(context, mode):
                for n in sorted(self.__dirty):
                    m, index = self.__banked.get(n, (None, None))
                    if m is not mode:
                        continue
                    if index is None:
                        await self.debug.set_execute(
                            A32.msr(0, spsr=True), self.__shadow[n])
                    else:
                        await self.debug.reg_set(index, self.__shadow[n])
        vfp = sorted(n for n in self.__dirty if n not in self.__banked)
        if vfp:
            async with self.__fpu_enabled():
                for n in vfp:
                    if n == CortexARegisters.FPSCR:
                        await self.debug.set_execute(A32.vmsr(0), self.__shadow[n])
                        continue
                    value = self.__shadow[n]
                    await self.debug.reg_set(0, value & 0xffffffff)
                    await self.debug.reg_set(1, value >> 32)
                    await self.debug.execute(
                        A32.vmov_to_d(n - CortexARegisters.D0, 0, 1))
        self.__shadow.clear()
        self.__dirty.clear()

    @contextlib.asynccontextmanager
    async def __in_mode(self, context, mode: Mode):
        """Switch to `mode` for banked register access and back to the
        halted one. Leaves r0 clobbered."""
        if mode is Mode.MON and (await self.debug.dscr()).ns:
            raise RuntimeError("Monitor mode is out of reach from Non-secure state")
        cpsr = context[self.CPSR]
        await self.debug.set_execute(A32.msr(0, PsrMask.C), (cpsr & ~0x1f) | mode)
        await self.debug.execute(A32.isb())
        try:
            yield
        finally:
            await self.debug.set_execute(A32.msr(0, PsrMask.C), cpsr)
            await self.debug.execute(A32.isb())

    @contextlib.asynccontextmanager
    async def __fpu_enabled(self):
        """Grant coprocessor access and enable the FPU for the time of
        an access, the halted program may have left it off. Leaves r0
        and r1 clobbered."""
        cpacr_access = A32.mrc(15, 0, 0, 1, 0, 2)
        cpacr = await self.debug.execute_get(cpacr_access)
        await self.debug.set_execute(A32.mcr(15, 0, 0, 1, 0, 2),
                                     cpacr | self.CPACR_CP10_CP11)
        await self.debug.execute(A32.isb())
        fpexc = await self.debug.execute_get(A32.vmrs(0, VfpSysReg.FPEXC))
        await self.debug.set_execute(A32.vmsr(0, VfpSysReg.FPEXC),
                                     fpexc | self.FPEXC_EN)
        try:
            yield
        finally:
            await self.debug.set_execute(A32.vmsr(0, VfpSysReg.FPEXC), fpexc)
            await self.debug.set_execute(A32.mcr(15, 0, 0, 1, 0, 2), cpacr)
            await self.debug.execute(A32.isb())

    async def caches_disable(self):
        """Turn the MMU, caches and branch prediction off, then drop
        stale instruction-side state. Dirty data lines are left in
        the disabled cache."""
        await self.halt()
        sctlr = await self.debug.execute_get(A32.mrc(15, 0, 0, 1, 0, 0))
        sctlr &= ~(self.SCTLR_M | self.SCTLR_C | self.SCTLR_Z | self.SCTLR_I)
        await self.debug.set_execute(A32.mcr(15, 0, 0, 1, 0, 0), sctlr)
        await self.debug.execute(A32.mcr(15, 0, 0, 7, 5, 0))   # ICIALLU
        await self.debug.execute(A32.mcr(15, 0, 0, 7, 5, 6))   # BPIALL
        await self.debug.execute(A32.mcr(15, 0, 0, 8, 7, 0))   # TLBIALL
        await self.debug.execute(A32.dsb())
        await self.debug.execute(A32.isb())

    async def dcache_clean_invalidate(self):
        """Clean and invalidate the level 1 data cache by set/way."""
        await self.halt()
        await self.debug.set_execute(A32.mcr(15, 2, 0, 0, 0, 0), 0)  # CSSELR
        await self.debug.execute(A32.isb())
        ccsidr = await self.debug.execute_get(A32.mrc(15, 1, 0, 0, 0, 0))
        line_shift = (ccsidr & 7) + 4
        ways = ((ccsidr >> 3) & 0x3ff) + 1
        sets = ((ccsidr >> 13) & 0x7fff) + 1
        way_shift = 32 - (ways - 1).bit_length()
        for way in range(ways):
            for index in range(sets):
                await self.debug.set_execute(
                    A32.mcr(15, 0, 0, 7, 14, 2),                  # DCCISW
                    way << way_shift | index << line_shift)
        await self.debug.execute(A32.dsb())

    async def resume(self, *, allow_interrupts=True):
        """A breakpoint on the resume address would hit again before
        the instruction executes, step over it first."""
        if self.__breakpoints_at((await self.halted_context())[self.PC]):
            await self.step()
        await super().resume(allow_interrupts=allow_interrupts)

    def __breakpoints_at(self, addr: int) -> list[int]:
        return [index for index, (a, _kind) in self.__breakpoints.items()
                if a == addr]

    async def step(self):
        """Resume with an address mismatch breakpoint on the current
        instruction and interrupts masked. The last breakpoint unit is
        kept for this."""
        context = await self.halted_context()
        pc = context[self.PC]
        if Psr(context[self.CPSR]).thumb:
            byte_select = 0b1100 if pc & 2 else 0b0011
        else:
            byte_select = 0b1111
        masked = self.__breakpoints_at(pc)
        for index in masked:
            await self.debug.breakpoint_clear(index)
        index = await self.__step_breakpoint()
        await self.debug.breakpoint_set(index, pc, Bcr(
            enable=True, privilege=PrivilegeMatch.ANY,
            byte_select=byte_select, type=BreakpointType.ADDRESS_MISMATCH))
        await self.debug.dscr_update(intdis=True)
        self.__stepping = True
        try:
            await super().resume()
            if not await self.wait_halted(self.STEP_TIMEOUT):
                await self.debug_halt()
            await self.halted_context()
        finally:
            self.__stepping = False
            await self.debug.breakpoint_clear(index)
            await self.debug.dscr_update(intdis=False)
            for index in masked:
                await self.__breakpoint_program(index)

    async def wait_halted(self, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await self.debug_halted():
            if loop.time() > deadline:
                return False
        return True

    # Breakpoints and watchpoints

    async def __step_breakpoint(self) -> int:
        if self.__breakpoint_count is None:
            self.__breakpoint_count = await self.debug.breakpoint_count()
        return self.__breakpoint_count - 1

    async def points_clear(self):
        """Disable every breakpoint and watchpoint unit, including
        those left by an earlier session."""
        for index in range(await self.__step_breakpoint() + 1):
            await self.debug.breakpoint_clear(index)
        if self.__watchpoint_count is None:
            self.__watchpoint_count = await self.debug.watchpoint_count()
        for index in range(self.__watchpoint_count):
            await self.debug.watchpoint_clear(index)
        self.__breakpoints.clear()
        self.__watchpoints.clear()

    async def breakpoint_add(self, addr, kind):
        """`kind` is the GDB Z-packet kind: 2 or 3 for Thumb, 4 for
        ARM."""
        if kind == 4:
            if addr & 3:
                raise ValueError(f"Unaligned ARM breakpoint at {addr:#x}")
        elif kind in (2, 3):
            if addr & 1:
                raise ValueError(f"Unaligned Thumb breakpoint at {addr:#x}")
        else:
            raise ValueError(f"Unknown breakpoint kind {kind}")
        free = [i for i in range(await self.__step_breakpoint())
                if i not in self.__breakpoints]
        if not free:
            raise RuntimeError("No free breakpoint unit")
        index = free[0]
        self.__breakpoints[index] = (addr, kind)
        await self.__breakpoint_program(index)
        return (index, addr, kind)

    async def __breakpoint_program(self, index: int):
        addr, kind = self.__breakpoints[index]
        if kind == 4:
            byte_select = 0b1111
        else:
            byte_select = 0b1100 if addr & 2 else 0b0011
        await self.debug.breakpoint_set(index, addr, Bcr(
            enable=True, privilege=PrivilegeMatch.ANY,
            byte_select=byte_select, type=BreakpointType.ADDRESS_MATCH))

    async def breakpoint_remove(self, bp):
        index, _addr, _kind = bp
        await self.debug.breakpoint_clear(index)
        del self.__breakpoints[index]

    async def breakpoint_list(self):
        return [(index, addr, kind)
                for index, (addr, kind) in self.__breakpoints.items()]

    @staticmethod
    def watch_span(addr: int, size: int) -> tuple[int, int]:
        """(byte select, mask) covering exactly `size` bytes at
        `addr`: bytes of one word, or an aligned power-of-two block of
        at least 8 bytes."""
        if 0 < size and (addr & 3) + size <= 4:
            return ((1 << size) - 1) << (addr & 3), 0
        if size >= 8 and size & (size - 1) == 0 and addr % size == 0:
            return 0b1111, size.bit_length() - 1
        raise ValueError(f"Cannot watch {size} bytes at {addr:#x}")

    async def watchpoint_add(self, addr, size, kind):
        """`kind` is the GDB Z-packet type: 2 write, 3 read, 4
        access."""
        try:
            access = self.__WATCH_ACCESS[kind]
        except KeyError:
            raise ValueError(f"Unknown watchpoint kind {kind}") from None
        byte_select, mask = self.watch_span(addr, size)
        if self.__watchpoint_count is None:
            self.__watchpoint_count = await self.debug.watchpoint_count()
        free = [i for i in range(self.__watchpoint_count)
                if i not in self.__watchpoints]
        if not free:
            raise RuntimeError("No free watchpoint unit")
        index = free[0]
        await self.debug.watchpoint_set(index, addr, Wcr(
            enable=True, privilege=PrivilegeMatch.ANY, access=access,
            byte_select=byte_select, mask=mask))
        self.__watchpoints[index] = (kind, addr, size)
        return (kind, addr, size)

    async def watchpoint_remove(self, wp):
        for index, point in self.__watchpoints.items():
            if point == tuple(wp):
                await self.debug.watchpoint_clear(index)
                del self.__watchpoints[index]
                return
        raise KeyError(f"Watchpoint {wp!r} not found")

    async def watchpoint_list(self):
        return list(self.__watchpoints.values())

    async def watchpoint_hit(self):
        """Watchpoint debug entry doesn't tell which unit matched, so
        only a lone watchpoint can be named."""
        if await self.halt_cause() != HaltCause.WATCHPOINT:
            return None
        if len(self.__watchpoints) != 1:
            return None
        return next(iter(self.__watchpoints.values()))

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
        sctlr = await self.debug.execute_get(A32.mrc(15, 0, 0, 1, 0, 0))
        if sctlr & (self.SCTLR_C | self.SCTLR_I):
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
