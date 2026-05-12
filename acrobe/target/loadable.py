"""Loadable — programming view on a Target.

A `Loadable` is a Node child of a `Target` that knows how to apply
a `MemoryMap` to one or more `Region` children. Targets with more
than one programmable surface (e.g. an FPGA with both volatile SRAM
config and on-die flash) have one `Loadable` per surface, named
distinctly.

The base `write()` orchestrates per-region planning and
per-page writes. Subclasses with whole-target stitching (option-byte
unlock+reload on STM32, secure-flash dance, …) override the
`pre_program` / `post_program` hooks rather than rewriting the loop.

Subclasses that don't operate on `Region`s at all — FpgaTarget's
volatile-config loader, ICE-attached chip-erase paths, etc. —
override `write` directly.
"""

from ..node import Node
from ..memory_map import MemoryMap
from .region import Flash, Region


async def coerce_memory_map(source):
    if isinstance(source, MemoryMap):
        return source
    if isinstance(source, Node):
        return await MemoryMap.from_node(source)
    raise TypeError(
        f"Loadable expects a MemoryMap or Node, "
        f"got {type(source).__name__}")


class Loadable(Node):
    """Programmable surface on a Target. Holds Region children."""

    def __init__(self, name="main"):
        super().__init__(name)

    @property
    def regions(self):
        return self.children_of_class(Region)

    async def write(self, source, *, do_erase=False, do_verify=False,
                    do_start=False, update=True, assume_clean=False):
        m = (await coerce_memory_map(source)).simplified()
        await self.pre_program(do_erase=do_erase, assume_clean=assume_clean)

        for region in sorted(self.regions):
            region_m = m.within(region.address, region.end)
            if not region_m:
                continue
            async for offset, data in region.plan_update(region_m):
                await region.write(offset, data)

        success = True
        if do_verify:
            success = await self.verify(m)

        await self.post_program(success=success, do_start=do_start)

    async def read(self, begin=0, end=None) -> MemoryMap:
        regions = sorted(self.regions)
        if end is None:
            end = max((r.end for r in regions), default=0)

        m = MemoryMap()
        for region in regions:
            lo = max(region.address, begin)
            hi = min(region.end, end)
            if lo >= hi:
                continue
            data = await region.read(lo - region.address, hi - lo)
            m.append(lo, bytes(data))
        return m

    async def verify(self, source) -> bool:
        m = (await coerce_memory_map(source)).simplified()
        for region in sorted(self.regions):
            region_m = m.within(region.address, region.end)
            if not region_m:
                continue
            for addr, data in region_m:
                actual = await region.read(addr - region.address, len(data))
                if actual != bytes(data):
                    self.logger.error(
                        "Mismatch in %s at 0x%08x", region.name, addr)
                    return False
        return True

    async def erase_all(self):
        for f in self.children_of_class(Flash):
            await f.erase(0, f.size)
            f.is_blank = True

    def force_blank(self):
        """Mark every Flash region as blank without erasing.

        Use only when external state guarantees the device is
        already blank (e.g. just power-up after JTAG mass erase by
        another tool)."""
        for f in self.children_of_class(Flash):
            f.is_blank = True

    async def pre_program(self, *, do_erase, assume_clean):
        if do_erase:
            await self.erase_all()
        if assume_clean:
            self.force_blank()

    async def post_program(self, *, success, do_start):
        if do_start and success:
            await self.reset()

    async def reset(self):
        """Reset the target after a successful program-and-start.
        Override in subclasses that own a reset path; default is a
        no-op so the framework doesn't crash on do_start=True for
        passive devices (SPI flash, EEPROM, …)."""
