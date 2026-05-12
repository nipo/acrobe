"""Flash Patch and Breakpoint (FPB). Cortex-M instruction-address
comparator + literal-patch block. Provides hardware breakpoints.

ARM PartId 0x003 (Cortex-M3/M4 FPB). The simpler BPU on Cortex-M0+
uses PartId 0x00B and exposes a subset of the FPB programmer's
model — same friendly label. ARMv8-M (M55/M85) advertises via
DEVARCH ARCHID = 0x1A03 (BPU; same role, no flash-patch).

The unit holds N code comparators (typically 6 or 8); a
breakpoint is set by writing the target address + REPLACE flags
to FP_COMP[i]. REPLACE selects which Thumb halfword in the
aligned 32-bit word the comparator matches — `addr & 2` decides
upper vs lower. CTRL must have both ENABLE and KEY set on every
write that should commit."""

from __future__ import annotations

import asyncio

from .model import DevArch, MemoryMappedComponent, PartId


@MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x1A03, revision=0, present=True))
@MemoryMappedComponent.db.register(PartId.from_idcode(0x003477))
@MemoryMappedComponent.db.register(PartId.from_idcode(0x00b477))
class Fpb(MemoryMappedComponent):
    FRIENDLY_NAME = "Flash Patch and Breakpoint"

    CTRL_OFFSET   = 0x000
    REMAP_OFFSET  = 0x004

    CTRL_KEY      = 1 << 1
    CTRL_ENABLE   = 1 << 0

    COMP_ENABLE   = 1 << 0
    COMP_REPLACE_REMAP = 0 << 30
    COMP_REPLACE_LOWER = 1 << 30
    COMP_REPLACE_UPPER = 2 << 30
    COMP_REPLACE_BOTH  = 3 << 30

    @staticmethod
    def comp_offset(index: int) -> int:
        return 0x008 + 4 * index

    def __init__(self, bus, base: int, ids, name: str | None = None):
        super().__init__(bus, base, ids, name=name)
        self.code_count = 0
        self.lit_count = 0
        self.allocations: dict[int, int] = {}

    async def start(self) -> None:
        ctrl = await self.reg_read(self.CTRL_OFFSET)
        self.code_count = ((ctrl >> 4) & 0xF) | (((ctrl >> 12) & 0x7) << 4)
        self.lit_count = (ctrl >> 8) & 0xF
        self.logger.info("%d code comparators, %d literal slots",
                         self.code_count, self.lit_count)

    async def enable(self, enabled: bool = True) -> None:
        word = (self.CTRL_KEY | self.CTRL_ENABLE) if enabled else self.CTRL_KEY
        await self.reg_write(self.CTRL_OFFSET, word)

    async def is_enabled(self) -> bool:
        ctrl = await self.reg_read(self.CTRL_OFFSET)
        return bool(ctrl & self.CTRL_ENABLE)

    async def comp_set(self, index: int, addr: int | None) -> None:
        """Program comparator `index`.

        `addr=None` disables the comparator. Otherwise the address
        is encoded with REPLACE selecting which halfword to match.
        ARMv8-M cores use the legacy ARMv7-M REPLACE encoding too —
        the simpler BPU implementations ignore REPLACE on write,
        so the legacy bits don't hurt."""
        if index >= self.code_count:
            raise ValueError(
                f"FPB comparator {index} out of range "
                f"(have {self.code_count})")
        if addr is None:
            await self.reg_write(self.comp_offset(index), 0)
            self.allocations.pop(index, None)
            return
        replace = self.COMP_REPLACE_UPPER if (addr & 2) else self.COMP_REPLACE_LOWER
        value = replace | (addr & 0x1FFFFFFC) | self.COMP_ENABLE
        await self.reg_write(self.comp_offset(index), value)
        self.allocations[index] = addr

    async def comp_clear(self) -> None:
        futures = [
            self.reg_write(self.comp_offset(i), 0)
            for i in range(self.code_count)
        ]
        await asyncio.gather(*futures)
        self.allocations.clear()

    def allocate(self) -> int | None:
        """Return a free comparator index, or None if all slots are used.

        Bookkeeping only — call `comp_set` afterwards to actually
        program the slot."""
        for i in range(self.code_count):
            if i not in self.allocations:
                self.allocations[i] = -1
                return i
        return None

    def release(self, index: int) -> None:
        self.allocations.pop(index, None)
