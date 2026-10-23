"""ARM9E-S / ARM9EJ-S debug logic: scan chain lowering and
pipeline primitives.

In debug state the core executes whatever instruction words are
shifted into scan chain 1, one pipeline stage per scan. Each
primitive here is a fixed instruction/data sequence whose timing
follows the five-stage ARM9 pipeline: an instruction scanned at
step N reaches Execute at step N+2, and its data bus transfers
happen on the following steps.
"""

import asyncio
from dataclasses import dataclass

from ....engine import Batcher, chain_future
from ....node import Node
from ..a32 import A32, PsrMask
from .embedded_ice import DebugControl, DebugStatus, IceRegister
from .scan_chain import Cp15Chain, DebugChain, IceChain, ScanChain


@dataclass(frozen=True, slots=True)
class IceRead:
    register: int

    def __repr__(self):
        return f"IceRead({self.register:#x})"


@dataclass(frozen=True, slots=True)
class IceWrite:
    register: int
    value: int

    def __repr__(self):
        return f"IceWrite({self.register:#x}, {self.value:#010x})"


@dataclass(frozen=True, slots=True)
class Cp15Read:
    opc1: int
    crn: int
    crm: int
    opc2: int

    def __repr__(self):
        return f"Cp15Read({self.opc1}, c{self.crn}, c{self.crm}, {self.opc2})"


@dataclass(frozen=True, slots=True)
class Cp15Write:
    opc1: int
    crn: int
    crm: int
    opc2: int
    value: int

    def __repr__(self):
        return (f"Cp15Write({self.opc1}, c{self.crn}, c{self.crm}, "
                f"{self.opc2}, {self.value:#010x})")


@dataclass(frozen=True, slots=True)
class DebugShift:
    """One scan chain 1 step: present `instr` on the instruction
    bus and `data` on the data bus, then clock the core once.
    Resolves to the captured data bus when `read`."""
    instr: int
    data: int = 0
    sysspeed: bool = False
    read: bool = False

    def __repr__(self):
        flags = (", sysspeed" if self.sysspeed else "") \
            + (", read" if self.read else "")
        return f"DebugShift({self.instr:#010x}, {self.data:#010x}{flags})"


@dataclass(frozen=True, slots=True)
class Restart:
    """Leave debug state through RESTART and Run-Test/Idle."""

    def __repr__(self):
        return "Restart()"


class Cp15AccessIncomplete(Exception):
    pass


class Arm9Lowerer:
    """Lowers one batch of debug ops to TAP shifts.

    EmbeddedICE reads return their value on the next scan chain 2
    shift, so consecutive ICE accesses overlap; a dummy status read
    collects the last one before leaving chain 2."""

    def __init__(self, tap, chain: ScanChain | None):
        self.tap = tap
        self.chain = chain
        self.pending = None

    def select(self, chain: ScanChain):
        if self.chain is not chain:
            self.ice_flush()
            self.tap.SCAN_N(int(chain), read_tdo=False)
            self.chain = chain

    def ice_shift(self, write: bool, register: int, value: int = 0):
        self.select(ScanChain.EMBEDDED_ICE)
        lower = self.tap.INTEST_ICE(IceChain.pack(write, register, value),
                                    read_tdo=self.pending is not None)
        if self.pending is not None:
            chain_future(lower, self.pending, IceChain.data)
            self.pending = None
        return lower

    def ice_flush(self):
        if self.pending is not None:
            self.ice_shift(False, IceRegister.DEBUG_STATUS)

    def ice_read(self, op: IceRead, upper):
        self.ice_shift(False, op.register)
        self.pending = upper

    def ice_write(self, op: IceWrite, upper):
        chain_future(self.ice_shift(True, op.register, op.value),
                     upper, lambda _: None)

    def cp15(self, request: int, upper, transform):
        self.select(ScanChain.CP15)
        self.tap.INTEST_CP15(request, read_tdo=False, post_dr_run=1)
        lower = self.tap.INTEST_CP15(Cp15Chain.nop(), read_tdo=True,
                                     post_dr_run=1)
        chain_future(lower, upper, transform)

    @staticmethod
    def cp15_result(tdo):
        if not Cp15Chain.completed(tdo):
            raise Cp15AccessIncomplete("CP15 access did not complete")
        return Cp15Chain.data(tdo)

    @classmethod
    def cp15_written(cls, tdo):
        cls.cp15_result(tdo)

    def cp15_read(self, op: Cp15Read, upper):
        self.cp15(Cp15Chain.pack(False, op.opc1, op.crn, op.crm, op.opc2),
                  upper, self.cp15_result)

    def cp15_write(self, op: Cp15Write, upper):
        self.cp15(Cp15Chain.pack(True, op.opc1, op.crn, op.crm, op.opc2,
                                 op.value),
                  upper, self.cp15_written)

    def debug_shift(self, op: DebugShift, upper):
        self.select(ScanChain.DEBUG)
        lower = self.tap.INTEST_DEBUG(
            DebugChain.pack(op.instr, op.data, op.sysspeed),
            read_tdo=op.read, post_dr_run=1)
        chain_future(lower, upper,
                     DebugChain.data if op.read else lambda _: None)

    def restart(self, op: Restart, upper):
        self.ice_flush()
        self.tap.RESTART(None, read_tdo=False)
        chain_future(self.tap.run(1), upper)

    def process(self, batch):
        handlers = {
            IceRead: self.ice_read,
            IceWrite: self.ice_write,
            Cp15Read: self.cp15_read,
            Cp15Write: self.cp15_write,
            DebugShift: self.debug_shift,
            Restart: self.restart,
        }
        for op, upper in batch:
            handler = handlers.get(type(op))
            if handler is None:
                upper.set_exception(
                    TypeError(f"Unhandled ARM9 debug op: {type(op).__name__}"))
                continue
            handler(op, upper)
        self.ice_flush()


class Arm9Debug(Batcher, Node):
    """Debug logic of an ARM9E-S family core, child of its
    :class:`~.tap.Arm9Tap`.

    The pipeline sequences post their ops synchronously, so each
    one reaches the TAP contiguous even with concurrent users.
    Sequences that move data through r0 say so; saving clobbered
    registers is the caller's job."""

    def __init__(self, name: str = "debug"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__chain: ScanChain | None = None

    def chain_forget(self):
        self.__chain = None

    async def flush_ops(self, batch):
        lowerer = Arm9Lowerer(self.parent, self.__chain)
        try:
            lowerer.process(batch)
        finally:
            self.__chain = lowerer.chain

    # EmbeddedICE

    async def ice_read(self, register: int) -> int:
        return await self.post(IceRead(register))

    async def ice_write(self, register: int, value: int):
        await self.post(IceWrite(register, int(value)))

    async def status(self) -> DebugStatus:
        return DebugStatus(await self.ice_read(IceRegister.DEBUG_STATUS))

    async def control(self) -> DebugControl:
        return DebugControl(await self.ice_read(IceRegister.DEBUG_CTRL))

    async def control_set(self, control: DebugControl):
        await self.ice_write(IceRegister.DEBUG_CTRL, int(control))

    async def wait_status(self, predicate, timeout: float = 1.0) -> DebugStatus:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = await self.status()
            if predicate(status):
                return status
            if loop.time() > deadline:
                raise TimeoutError(f"Debug status stuck at {status}")
            await asyncio.sleep(0.01)

    # CP15

    async def cp15_read(self, opc1: int, crn: int, crm: int, opc2: int) -> int:
        return await self.post(Cp15Read(opc1, crn, crm, opc2))

    async def cp15_write(self, opc1: int, crn: int, crm: int, opc2: int,
                         value: int):
        await self.post(Cp15Write(opc1, crn, crm, opc2, value))

    # Pipeline sequences

    def shift(self, instr: int, data: int = 0, *, sysspeed: bool = False,
              read: bool = False) -> asyncio.Future:
        return self.post(DebugShift(instr, data, sysspeed, read))

    def nop(self, data: int = 0, *, sysspeed: bool = False,
            read: bool = False) -> asyncio.Future:
        return self.shift(A32.nop(), data, sysspeed=sysspeed, read=read)

    def post_regs_read(self, regs) -> list[asyncio.Future]:
        """Read core registers of the current mode through
        `stmia r0, {regs}`. pc reads as the debug-state pipeline
        address, not the halted address."""
        regs = sorted(set(regs))
        self.shift(A32.stmia(0, regs))
        self.nop()
        self.nop()
        return [self.nop(read=True) for _ in regs]

    def post_regs_write(self, values: dict[int, int]) -> asyncio.Future:
        """Write core registers r0-r14 of the current mode through
        `ldmia r0, {regs}`."""
        if A32.PC in values:
            raise ValueError("Use post_pc_write for pc")
        regs = sorted(values)
        self.shift(A32.ldmia(0, regs))
        self.nop()
        self.nop()
        for r in regs:
            self.nop(values[r])
        return self.nop()

    def post_pc_write(self, pc: int) -> asyncio.Future:
        """Load pc. Execution resumes there through
        :meth:`post_branch_resume`."""
        self.shift(A32.ldmia(0, [A32.PC]))
        self.nop()
        self.nop()
        self.nop(pc)
        self.nop()
        return self.nop()

    def post_psr_read(self, spsr: bool = False) -> asyncio.Future:
        """Read CPSR or SPSR. Clobbers r0."""
        self.shift(A32.mrs(0, spsr=spsr))
        for _ in range(4):
            self.nop()
        self.shift(A32.str(0, A32.PC))
        self.nop()
        self.nop()
        return self.nop(read=True)

    def post_psr_write(self, value: int, spsr: bool = False) -> asyncio.Future:
        """Write CPSR or SPSR byte by byte through immediate MSRs, so
        no core register is used."""
        def msr(byte: int, mask: PsrMask) -> int:
            return A32.msr_imm(value & (0xff << (8 * byte)), mask, spsr=spsr)
        self.shift(msr(0, PsrMask.C))
        self.shift(msr(1, PsrMask.X))
        self.shift(msr(2, PsrMask.S))
        self.nop()
        self.nop()
        self.shift(msr(3, PsrMask.F))
        for _ in range(5):
            self.nop()
        return self.nop()

    def post_system_speed(self, instr: int) -> asyncio.Future:
        """Execute `instr`, a memory access, at system speed. The
        core leaves debug state and re-enters it once the access
        completes. Resolves to the debug status read right after the
        restart: unless both `syscomp` and `dbgack` are set, the
        access was still pending and the pipeline state is lost."""
        self.shift(instr)
        self.nop(sysspeed=True)
        self.post(Restart())
        status = asyncio.get_running_loop().create_future()
        chain_future(self.post(IceRead(IceRegister.DEBUG_STATUS)),
                     status, DebugStatus)
        return status

    def post_load_words(self, count: int):
        """Load `count` words (up to 14) from the address in r0 into
        r1.., post-incrementing r0. Returns the system speed status
        future and the loaded value futures."""
        regs = self.__data_registers(count)
        status = self.post_system_speed(A32.ldmia(0, regs, writeback=True))
        return status, self.post_regs_read(regs)

    def post_store_words(self, values):
        """Store words (up to 14) to the address in r0 through r1..,
        post-incrementing r0. Returns the system speed status
        future."""
        regs = self.__data_registers(len(values))
        self.post_regs_write(dict(zip(regs, values)))
        return self.post_system_speed(A32.stmia(0, regs, writeback=True))

    __LOADS = {1: A32.ldrb, 2: A32.ldrh, 4: A32.ldr}
    __STORES = {1: A32.strb, 2: A32.strh, 4: A32.str}

    def post_load_units(self, width: int, count: int):
        """Load `count` (up to 14) `width`-byte units from the address
        in r0, one access each, post-incrementing r0. Returns the
        system speed status futures and the loaded value futures."""
        regs = self.__data_registers(count)
        load = self.__LOADS[width]
        statuses = [self.post_system_speed(load(r, 0, width, post_index=True))
                    for r in regs]
        return statuses, self.post_regs_read(regs)

    def post_store_units(self, width: int, values):
        """Store `width`-byte units (up to 14) to the address in r0, one
        access each, post-incrementing r0. Returns the system speed
        status futures."""
        regs = self.__data_registers(len(values))
        store = self.__STORES[width]
        self.post_regs_write(dict(zip(regs, values)))
        return [self.post_system_speed(store(r, 0, width, post_index=True))
                for r in regs]

    @staticmethod
    def __data_registers(count: int) -> list[int]:
        if not 1 <= count <= 14:
            raise ValueError(f"Cannot move {count} registers through r1-r14")
        return list(range(1, count + 1))

    def post_branch_resume(self) -> asyncio.Future:
        """Leave debug state and resume at the pc written by
        :meth:`post_pc_write`: the branch lands two words back, on the
        pipeline's fetch address."""
        self.shift(A32.b(-8))
        self.nop(sysspeed=True)
        return self.post(Restart())

    def post_restart(self) -> asyncio.Future:
        return self.post(Restart())
