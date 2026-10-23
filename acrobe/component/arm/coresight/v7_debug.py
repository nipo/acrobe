"""ARMv7-A/R memory-mapped processor debug (v7 Debug).

Halting debug through DBGDRCR, instruction execution through
DBGITR and data exchange through the DCC registers DBGDTRRX /
DBGDTRTX. Core registers and memory are reached by executing
instructions in Debug state; this component only provides those
primitives, they all clobber core registers."""

from __future__ import annotations

import asyncio
import enum

from ....bitfield import Bitfield, BooleanField, EnumField, Field
from ....part_id import PartId
from ..a32 import A32
from .dbg import Dbg
from .model import MemoryMappedComponent


class MethodOfEntry(enum.IntEnum):
    HALT_REQUEST = 0x0
    BREAKPOINT = 0x1
    ASYNC_WATCHPOINT = 0x2
    BKPT = 0x3
    EXTERNAL = 0x4
    VECTOR_CATCH = 0x5
    OS_UNLOCK = 0x8
    SYNC_WATCHPOINT = 0xa


class ExtDccMode(enum.IntEnum):
    NON_BLOCKING = 0
    STALL = 1
    FAST = 2


class Dscr(Bitfield):
    halted = BooleanField(0)
    restarted = BooleanField(1)
    moe = EnumField(2, 4, MethodOfEntry)
    sdabort_l = BooleanField(6)
    adabort_l = BooleanField(7)
    und_l = BooleanField(8)
    dbgack = BooleanField(10)
    intdis = BooleanField(11)
    udccdis = BooleanField(12)
    itren = BooleanField(13)
    hdbgen = BooleanField(14)
    mdbgen = BooleanField(15)
    spiddis = BooleanField(16)
    spniddis = BooleanField(17)
    ns = BooleanField(18)
    adadiscard = BooleanField(19)
    extdccmode = EnumField(20, 2, ExtDccMode)
    instrcompl_l = BooleanField(24)
    pipeadv = BooleanField(25)
    txfull_l = BooleanField(26)
    rxfull_l = BooleanField(27)
    txfull = BooleanField(29)
    rxfull = BooleanField(30)
    all = Field(0, 32)


class Drcr(Bitfield):
    halt = BooleanField(0)
    restart = BooleanField(1)
    clear_sticky_exceptions = BooleanField(2)
    clear_sticky_pipeline_advance = BooleanField(3)
    cancel_bus_requests = BooleanField(4)


class Didr(Bitfield):
    revision = Field(0, 4)
    variant = Field(4, 4)
    version = Field(16, 4)
    ctx_cmps = Field(20, 4)
    brps = Field(24, 4)
    wrps = Field(28, 4)


class Prsr(Bitfield):
    powered_up = BooleanField(0)
    sticky_powerdown = BooleanField(1)
    reset = BooleanField(2)
    sticky_reset = BooleanField(3)


class PrivilegeMatch(enum.IntEnum):
    PRIVILEGED = 1
    USER = 2
    ANY = 3


class BreakpointType(enum.IntEnum):
    ADDRESS_MATCH = 0
    ADDRESS_MISMATCH = 4


class Bcr(Bitfield):
    enable = BooleanField(0)
    privilege = EnumField(1, 2, PrivilegeMatch)
    byte_select = Field(5, 4)
    type = EnumField(20, 3, BreakpointType)


class WatchAccess(enum.IntEnum):
    LOAD = 1
    STORE = 2
    ANY = 3


class Wcr(Bitfield):
    enable = BooleanField(0)
    privilege = EnumField(1, 2, PrivilegeMatch)
    access = EnumField(3, 2, WatchAccess)
    byte_select = Field(5, 4)
    mask = Field(24, 5)


class V7DebugAbort(IOError):
    pass


@MemoryMappedComponent.db.register(PartId.from_idcode(0xC09477))
class V7Debug(Dbg):
    FRIENDLY_NAME = "Cortex-A9 Debug"

    DIDR = 0x000
    WFAR = 0x018
    VCR = 0x01c
    DTRRX = 0x080
    ITR = 0x084
    DSCR = 0x088
    DTRTX = 0x08c
    DRCR = 0x090
    BVR = 0x100
    BCR = 0x140
    WVR = 0x180
    WCR = 0x1c0
    OSLAR = 0x300
    PRSR = 0x314
    LAR = 0xfb0

    LAR_KEY = 0xc5acce55

    TIMEOUT = 0.5

    # DSCR as of its last read. Its latched flags gate the next ITR
    # and DTR accesses: in Non-blocking mode, an ITR write is ignored
    # unless InstrCompl_l was set, a DTRRX write unless RXfull_l was
    # clear, a DTRTX read unless TXfull_l was set. Accesses whose
    # gate is known open are issued together with the DSCR read that
    # checks their outcome.
    __latched: Dscr | None = None

    # DCC transfers between a core register and DBGDTRRX/DBGDTRTX.
    @staticmethod
    def dtr_to_reg(rt: int) -> int:
        return A32.mrc(14, 0, rt, 0, 5, 0)

    @staticmethod
    def reg_to_dtr(rt: int) -> int:
        return A32.mcr(14, 0, rt, 0, 5, 0)

    async def dscr(self) -> Dscr:
        return self.__latch(await self.reg_read(self.DSCR))

    def __latch(self, value: int) -> Dscr:
        self.__latched = Dscr(value)
        return Dscr(value)

    async def __ready(self, *, rx_free: bool = False):
        def ready(d):
            return d.instrcompl_l and not (rx_free and d.rxfull_l)
        if self.__latched is None or not ready(self.__latched):
            await self.wait_dscr(ready)

    async def didr(self) -> Didr:
        return Didr(await self.reg_read(self.DIDR))

    async def prsr(self) -> Prsr:
        return Prsr(await self.reg_read(self.PRSR))

    async def dscr_update(self, **fields):
        dscr = await self.dscr()
        for k, v in fields.items():
            setattr(dscr, k, v)
        await self.reg_write(self.DSCR, int(dscr))

    async def drcr(self, **fields):
        await self.reg_write(self.DRCR, int(Drcr(**fields)))

    async def wait_dscr(self, predicate, timeout: float | None = None) -> Dscr:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.TIMEOUT if timeout is None else timeout)
        while True:
            dscr = await self.dscr()
            if predicate(dscr):
                return dscr
            if loop.time() > deadline:
                raise TimeoutError(f"{self.name}: DSCR stuck at {dscr}")

    async def breakpoint_count(self) -> int:
        return (await self.didr()).brps + 1

    async def watchpoint_count(self) -> int:
        return (await self.didr()).wrps + 1

    async def breakpoint_set(self, index: int, address: int, bcr: Bcr):
        await self.reg_write(self.BCR + 4 * index, 0)
        await self.reg_write(self.BVR + 4 * index, address & ~3)
        await self.reg_write(self.BCR + 4 * index, int(bcr))

    async def breakpoint_clear(self, index: int):
        await self.reg_write(self.BCR + 4 * index, 0)

    async def watchpoint_set(self, index: int, address: int, wcr: Wcr):
        await self.reg_write(self.WCR + 4 * index, 0)
        await self.reg_write(self.WVR + 4 * index, address & ~3)
        await self.reg_write(self.WCR + 4 * index, int(wcr))

    async def watchpoint_clear(self, index: int):
        await self.reg_write(self.WCR + 4 * index, 0)

    async def unlock(self):
        await self.reg_write(self.LAR, self.LAR_KEY)
        await self.reg_write(self.OSLAR, 0)

    # Run control

    async def halt(self) -> Dscr:
        await self.drcr(halt=True)
        return await self.wait_dscr(lambda d: d.halted)

    async def restart(self) -> Dscr:
        await self.dscr_update(itren=False)
        await self.drcr(clear_sticky_exceptions=True, restart=True)
        return await self.wait_dscr(lambda d: d.restarted)

    async def enter(self) -> Dscr:
        """Prepare a halted core for instruction execution: enable
        the ITR, and select non-blocking DCC mode with imprecise
        aborts discarded so an abort can't wedge the core. Halting
        debug mode is enabled too, breakpoints and watchpoints only
        halt the core with it."""
        await self.dscr_update(hdbgen=True, itren=True, adadiscard=True,
                               extdccmode=ExtDccMode.NON_BLOCKING)
        return await self.dscr()

    # Instruction execution

    async def execute(self, instr: int) -> Dscr:
        dscr, _ = await self.__issue(instr)
        return dscr

    async def __issue(self, instr: int, *, read_dtr: bool = False):
        """Execute `instr` and, with `read_dtr`, read DTRTX in the same
        go. The value is None when the read came too early."""
        await self.__ready()
        write = self.reg_write(self.ITR, instr)
        status = self.reg_read(self.DSCR)
        dtr = self.reg_read(self.DTRTX) if read_dtr else None
        await write
        dscr = self.__latch(await status)
        value = await dtr if dtr is not None else None
        if not (dscr.instrcompl_l and dscr.txfull_l):
            value = None
        if not dscr.instrcompl_l:
            dscr = await self.wait_dscr(lambda d: d.instrcompl_l)
        if dscr.sdabort_l or dscr.und_l:
            await self.drcr(clear_sticky_exceptions=True)
            raise V7DebugAbort(
                f"{self.name}: {instr:#010x} raised "
                f"{'abort' if dscr.sdabort_l else 'undefined'}")
        return dscr, value

    async def dtr_write(self, value: int):
        await self.__ready(rx_free=True)
        await self.reg_write(self.DTRRX, value & 0xffffffff)

    async def dtr_read(self) -> int:
        if self.__latched is None or not self.__latched.txfull_l:
            await self.wait_dscr(lambda d: d.txfull_l)
        self.__latched.txfull_l = False
        return await self.reg_read(self.DTRTX)

    async def reg_get(self, rt: int) -> int:
        """Read core register r0-r14 of the current mode."""
        _, value = await self.__issue(self.reg_to_dtr(rt), read_dtr=True)
        if value is None:
            value = await self.dtr_read()
        return value

    async def reg_set(self, rt: int, value: int):
        """Write core register r0-r14 of the current mode."""
        await self.__ready(rx_free=True)
        write = self.reg_write(self.DTRRX, value & 0xffffffff)
        await self.__issue(self.dtr_to_reg(rt))
        await write

    async def execute_get(self, instr: int, rt: int = 0) -> int:
        """Execute `instr`, then read `rt`."""
        await self.execute(instr)
        return await self.reg_get(rt)

    async def set_execute(self, instr: int, value: int, rt: int = 0):
        """Load `value` in `rt`, then execute `instr`."""
        await self.reg_set(rt, value)
        await self.execute(instr)

    # Memory, through r0 as the address register

    # Word runs use Fast DCC mode: the latched instruction is reissued
    # on every DTR access, which stalls on the APB side until the
    # previous one completed, so the accesses are issued back to back
    # without polling DSCR. An abort makes the following DTR accesses
    # no-ops, it is reported once the run is over.

    async def load_words(self, address: int, count: int) -> list[int]:
        ldc = A32.ldc(14, 5, 0, 4, post_index=True)
        await self.reg_set(0, address)
        await self.execute(ldc)
        values = []
        if count > 1:
            await self.dscr_update(extdccmode=ExtDccMode.FAST)
            try:
                await self.reg_write(self.ITR, ldc)
                values = await asyncio.gather(
                    *[self.reg_read(self.DTRTX) for _ in range(count - 1)])
                await self.__fast_run_end()
            finally:
                await self.dscr_update(extdccmode=ExtDccMode.NON_BLOCKING)
        return list(values) + [await self.dtr_read()]

    async def store_words(self, address: int, values):
        values = list(values)
        await self.reg_set(0, address)
        await self.dscr_update(extdccmode=ExtDccMode.FAST)
        try:
            await self.reg_write(self.ITR, A32.stc(14, 5, 0, 4, post_index=True))
            await asyncio.gather(
                *[self.reg_write(self.DTRRX, v & 0xffffffff) for v in values])
            await self.__fast_run_end()
        finally:
            await self.dscr_update(extdccmode=ExtDccMode.NON_BLOCKING)

    async def __fast_run_end(self):
        dscr = await self.wait_dscr(lambda d: d.instrcompl_l)
        if dscr.sdabort_l:
            await self.drcr(clear_sticky_exceptions=True)
            raise V7DebugAbort(f"{self.name}: abort in memory run")

    __LOADS = {1: A32.ldrb, 2: A32.ldrh}
    __STORES = {1: A32.strb, 2: A32.strh}

    async def load_units(self, address: int, width: int, count: int) -> list[int]:
        """Load `width`-byte units through r1, one access each."""
        if width == 4:
            return await self.load_words(address, count)
        await self.reg_set(0, address)
        load = self.__LOADS[width]
        values = []
        for _ in range(count):
            await self.execute(load(1, 0, width, post_index=True))
            values.append(await self.reg_get(1))
        return values

    async def store_units(self, address: int, width: int, values):
        if width == 4:
            return await self.store_words(address, values)
        await self.reg_set(0, address)
        store = self.__STORES[width]
        for v in values:
            await self.reg_set(1, v)
            await self.execute(store(1, 0, width, post_index=True))
