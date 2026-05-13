"""Data Watchpoint and Trace (DWT). Cortex-M data-watchpoint and
PC-sample / exception-trace block.

ARM PartId 0x002 (Cortex-M3/M4 DWT). ARMv8-M advertises via
DEVARCH ARCHID = 0x1A02.

This module exposes the comparator block as hardware watchpoints.
Each comparator pairs an address (COMP), a size-as-power-of-two
mask (MASK), and a mode (FUNCTION). The mode selects what counts
as a match — instruction fetch, data read, data write, or any
data access. CTRL[31:28] holds the number of comparators
(typically 2 on M0+, 4 on M3/M4, more on M7/M33).

Cycle counter / PC sampling / exception trace are out of scope
here — only what watchpoints need to land.
"""

from __future__ import annotations

import asyncio

from .model import DevArch, MemoryMappedComponent, PartId


@MemoryMappedComponent.db.register(PartId.from_idcode(0x002477))
@MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x1A02, revision=0, present=True))
class Dwt(MemoryMappedComponent):
    FRIENDLY_NAME = "Data Watchpoint and Trace"

    CTRL_OFFSET = 0x000
    CTRL_NUMCOMP_SHIFT = 28

    @staticmethod
    def comp_offset(index: int) -> int:
        return 0x020 + 0x10 * index

    @staticmethod
    def mask_offset(index: int) -> int:
        return 0x024 + 0x10 * index

    @staticmethod
    def function_offset(index: int) -> int:
        return 0x028 + 0x10 * index

    # FUNCTION values for data-address watchpoints.
    # ARMv7-M ARM Table C1-15.
    FUNC_DISABLED         = 0x0
    FUNC_PC_MATCH         = 0x4
    FUNC_DATA_READ        = 0x5
    FUNC_DATA_WRITE       = 0x6
    FUNC_DATA_ACCESS      = 0x7

    def __init__(self, bus, base: int, ids, name: str | None = None):
        super().__init__(bus, base, ids, name=name)
        self.comparator_count = 0
        self.allocations: dict[int, int] = {}

    async def start(self) -> None:
        ctrl = await self.reg_read(self.CTRL_OFFSET)
        self.comparator_count = (ctrl >> self.CTRL_NUMCOMP_SHIFT) & 0xF
        self.logger.info("%d comparators", self.comparator_count)

    async def comp_set(self, index: int, *,
                       addr: int, size: int, function: int) -> None:
        """Program comparator `index` for a data-address watchpoint.

        `size` is the watched span in bytes — must be a power of
        two between 1 and 32768 (the MASK field is log2(size), 5
        bits, 0..15). `function` is one of FUNC_* values; pass
        FUNC_DISABLED via `comp_clear` rather than here."""
        if index >= self.comparator_count:
            raise ValueError(
                f"DWT comparator {index} out of range "
                f"(have {self.comparator_count})")
        if function == self.FUNC_DISABLED:
            raise ValueError("Use comp_clear for FUNC_DISABLED")
        mask_log = (size & -size).bit_length() - 1
        if size != (1 << mask_log) or mask_log > 15:
            raise ValueError(
                f"DWT MASK must be a power of two in [1, 32768]; "
                f"got size={size}")
        await asyncio.gather(
            self.reg_write(self.comp_offset(index), addr),
            self.reg_write(self.mask_offset(index), mask_log),
            self.reg_write(self.function_offset(index), function),
        )
        self.allocations[index] = function

    async def comp_clear(self, index: int) -> None:
        if index >= self.comparator_count:
            raise ValueError(
                f"DWT comparator {index} out of range "
                f"(have {self.comparator_count})")
        await self.reg_write(
            self.function_offset(index), self.FUNC_DISABLED)
        self.allocations.pop(index, None)

    def allocate(self) -> int | None:
        """Return a free comparator index, or None if all slots used.

        Bookkeeping only — `comp_set` actually programs it. Marker
        value -1 reserves the slot before configuration so concurrent
        callers don't pick the same one."""
        for i in range(self.comparator_count):
            if i not in self.allocations:
                self.allocations[i] = -1
                return i
        return None

    def release(self, index: int) -> None:
        self.allocations.pop(index, None)
