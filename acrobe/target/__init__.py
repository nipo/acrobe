"""Target framework.

A Target is an agglomeration of memory regions (Flash, RAM, EEPROM)
that can be programmed. It dispatches addressed bytes to the
appropriate child Region, orchestrating erase/write/verify
operations.

`Target.write` accepts either a `MemoryMap` or a started VFS Node
subtree (the latter is converted via `MemoryMap.from_node`).
`Target.read` returns a `MemoryMap`.
"""

from ..node import Node
from .memory import Region, Flash
from ..memory_map import MemoryMap


async def _coerce_memory_map(arg):
    """Accept a MemoryMap or a Node, return a MemoryMap."""
    if isinstance(arg, MemoryMap):
        return arg
    if isinstance(arg, Node):
        return await MemoryMap.from_node(arg)
    raise TypeError(
        f"Target write/verify expects a MemoryMap or Node, "
        f"got {type(arg).__name__}")


class Target(Node):
    """Base target class.

    Subclasses represent specific SoCs or board configurations.
    They add Region children representing memory banks.

    Target.write() dispatches a MemoryMap to child regions,
    handling erase/write/verify orchestration.
    """

    _explorers = []

    @classmethod
    def register(cls, *component_types, precedence=1000):
        """Decorator: register a target discovery function.

        The decorated function is called with a matching component
        to instantiate the target. Lower precedence = higher priority.
        """
        def decorator(func):
            cls._explorers.append(_Explorer(func, component_types, precedence))
            cls._explorers.sort(key=lambda e: e.precedence)
            return func
        return decorator

    async def write(self, source, *, do_erase=False, do_verify=False,
                    do_start=False, update=True, assume_clean=False):
        """Program the target with `source` (MemoryMap or Node)."""
        m = await _coerce_memory_map(source)
        if do_erase:
            await self.erase_all()
        if assume_clean:
            self._force_blank()

        m = m.simplified()
        regions = sorted(self.children_of_class(Region))

        to_erase = []
        to_write = []

        for region in regions:
            region_m = m.within(region.address, region.end)
            if not region_m:
                continue

            # Collect erase operations for non-blank regions
            if isinstance(region, Flash) and not region.is_blank:
                for addr, data in region_m:
                    offset = addr - region.address
                    to_erase.append((region, offset, len(data)))

            # Collect write operations (paged for Flash)
            if isinstance(region, Flash):
                paged = region_m.paged(region.write_page_size)
                for addr, data in paged:
                    to_write.append((region, addr - region.address, data))
            else:
                for addr, data in region_m:
                    to_write.append((region, addr - region.address, data))

        # Erase
        with self.progress("Erasing", len(to_erase), "regions") as p:
            for region, offset, size in to_erase:
                await region.erase(offset, size)
                p.advance()

        # Write
        with self.progress("Writing", len(to_write), "pages") as p:
            for region, offset, data in to_write:
                await region.write(offset, data)
                p.advance()

        success = True
        if do_verify:
            success = await self.verify(m)

        if do_start and success:
            await self.reset()

    async def verify(self, source):
        """Verify programmed data against `source` (MemoryMap or Node)."""
        m = (await _coerce_memory_map(source)).simplified()
        regions = sorted(self.children_of_class(Region))

        for region in regions:
            region_m = m.within(region.address, region.end)
            if not region_m:
                continue
            for addr, data in region_m:
                offset = addr - region.address
                actual = await region.read(offset, len(data))
                if actual != bytes(data):
                    self.logger.error("Mismatch in %s at 0x%08x",
                                      region.name, addr)
                    return False
        return True

    async def read(self, begin=0, end=None) -> MemoryMap:
        """Read all readable regions into a MemoryMap."""
        regions = sorted(self.children_of_class(Region))
        if end is None:
            end = max((r.end for r in regions), default=0)

        m = MemoryMap()
        for region in regions:
            r_start = max(region.address, begin)
            r_end = min(region.end, end)
            if r_start >= r_end:
                continue
            offset = r_start - region.address
            size = r_end - r_start
            data = await region.read(offset, size)
            m.chunks.append((r_start, bytes(data)))
        return m

    async def erase_all(self):
        """Erase all flash regions."""
        flashes = self.children_of_class(Flash)
        for f in flashes:
            await f.erase(0, f.size)
        self._force_blank()

    async def reset(self):
        """Reset the target. Override in subclass."""
        pass

    def _force_blank(self):
        """Mark all flash regions as blank."""
        for f in self.children_of_class(Flash):
            f.is_blank = True


class _Explorer:
    """Registered target discovery function."""

    def __init__(self, func, component_types, precedence):
        self.func = func
        self.component_types = component_types
        self.precedence = precedence


class Field(Node):
    """Target discovery: walks component tree, matches explorers to components.

    Creates Target children for each discovered component.
    After discover(), self.unhandled contains components not claimed by any explorer.
    """

    def __init__(self):
        super().__init__("Targets")
        self.unhandled = set()

    async def discover(self, *roots):
        types = set()
        for e in Target._explorers:
            types |= set(e.component_types)

        interests = {}
        for t in types:
            interests[t] = set()
            for root in roots:
                interests[t] |= set(
                    root.children_of_class(t, include_self=True))

        for explorer in Target._explorers:
            for comp_type in explorer.component_types:
                for component in list(interests.get(comp_type, [])):
                    try:
                        target = explorer.func(component)
                    except NotImplementedError:
                        continue
                    self.child_add(target)
                    to_remove = set(
                        component.children_find(lambda x: True))
                    for k in interests:
                        interests[k] -= to_remove

        self.unhandled = set()
        for v in interests.values():
            self.unhandled |= v


from . import fpga  # noqa: F401, E402
from . import spi_flash  # noqa: F401, E402
