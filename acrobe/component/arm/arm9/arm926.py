"""ARM926EJ-S memory system control through scan chain 15."""

from ....bitfield import Bitfield, BooleanField, Field
from .debug import Arm9Debug


class Arm926Control(Bitfield):
    """CP15 c1 control register. `all` spans the word so that
    reserved should-be-one bits survive read-modify-write."""
    all = Field(0, 32)
    mmu = BooleanField(0)
    alignment = BooleanField(1)
    dcache = BooleanField(2)
    big_endian = BooleanField(7)
    system = BooleanField(8)
    rom = BooleanField(9)
    icache = BooleanField(12)
    high_vectors = BooleanField(13)
    round_robin = BooleanField(14)
    l4 = BooleanField(15)


class Arm926DebugOverride(Bitfield):
    """CP15 c15 debug override register."""
    all = Field(0, 32)
    test_and_clean_all = BooleanField(19)


class Arm926Cp15:
    """CP15 operations of an ARM926EJ-S, issued over scan chain 15
    so they need no instruction stuffing."""

    DCACHE_CLEAN = 1 << 30

    def __init__(self, debug: Arm9Debug):
        self.debug = debug

    async def id(self) -> int:
        return await self.debug.cp15_read(0, 0, 0, 0)

    async def control(self) -> Arm926Control:
        return Arm926Control(await self.debug.cp15_read(0, 1, 0, 0))

    async def control_set(self, control: Arm926Control):
        await self.debug.cp15_write(0, 1, 0, 0, int(control))

    async def dcache_clean_invalidate(self):
        """Write back and invalidate the whole DCache in one test,
        clean and invalidate operation, which the debug override
        "test and clean all" bit extends to the complete cache."""
        saved = Arm926DebugOverride(await self.debug.cp15_read(0, 15, 0, 0))
        override = Arm926DebugOverride(int(saved))
        override.test_and_clean_all = True
        await self.debug.cp15_write(0, 15, 0, 0, int(override))
        try:
            status = await self.debug.cp15_read(0, 7, 14, 3)
        finally:
            await self.debug.cp15_write(0, 15, 0, 0, int(saved))
        if not status & self.DCACHE_CLEAN:
            raise IOError("DCache still dirty after test, clean and invalidate")

    async def write_buffer_drain(self):
        await self.debug.cp15_write(0, 7, 10, 4, 0)

    async def icache_invalidate(self):
        await self.debug.cp15_write(0, 7, 5, 0, 0)

    async def caches_disable(self):
        """Turn MMU and caches off with memory kept coherent: dirty
        DCache lines are written back first and the ICache is
        invalidated so code written afterwards is fetched afresh."""
        control = await self.control()
        if control.dcache:
            await self.dcache_clean_invalidate()
        await self.write_buffer_drain()
        await self.icache_invalidate()
        control.mmu = False
        control.dcache = False
        control.icache = False
        await self.control_set(control)
