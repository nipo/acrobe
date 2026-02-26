"""Target framework.

A Target is an agglomeration of memory regions (Flash, RAM, EEPROM)
that can be programmed. It dispatches program segments to the appropriate
child Region, orchestrating erase/write/verify operations.
"""

from ..component import Component
from .memory import Region, Flash

from ..loadable import Program, Segment


class Target(Component):
    """Base target class.

    Subclasses represent specific SoCs or board configurations.
    They add Region children representing memory banks.

    Target.write() dispatches program segments to child regions,
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

    async def write(self, program, *, do_erase=False, do_verify=False,
                    do_start=False, update=True, assume_clean=False):
        """Program the target with the given program.

        Args:
            program: Program with segments to write
            do_erase: erase all flash regions first
            do_verify: verify after writing
            do_start: reset after successful write
            update: use CRC-based selective update (skip unchanged pages)
            assume_clean: treat all flash as blank (skip erase)
        """
        if do_erase:
            await self.erase_all()
        if assume_clean:
            self._force_blank()

        program = program.simplified()
        regions = sorted(self.children_of_class(Region))

        to_erase = []
        to_write = []

        for region in regions:
            region_program = program.within(region.address, region.end)
            if not region_program:
                continue

            # Collect erase operations for non-blank regions
            if isinstance(region, Flash) and not region.is_blank:
                for seg in region_program:
                    offset = seg.address - region.address
                    to_erase.append((region, offset, len(seg)))

            # Collect write operations (paged for Flash)
            if isinstance(region, Flash):
                paged = region_program.paged(region.write_page_size)
                for seg in paged:
                    to_write.append((region, seg.address - region.address, seg.data))
            else:
                for seg in region_program:
                    to_write.append((region, seg.address - region.address, seg.data))

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
            success = await self.verify(program)

        if do_start and success:
            await self.reset()

    async def verify(self, program):
        """Verify programmed data against expected program content."""
        program = program.simplified()
        regions = sorted(self.children_of_class(Region))

        for region in regions:
            region_program = program.within(region.address, region.end)
            if not region_program:
                continue

            for seg in region_program:
                offset = seg.address - region.address
                actual = await region.read(offset, len(seg))
                if actual != bytes(seg.data):
                    self.logger.error("Mismatch in %s at 0x%08x",
                                      region.name, seg.address)
                    return False
        return True

    async def read(self, begin=0, end=None):
        """Read all readable regions into a Program."""
        regions = sorted(self.children_of_class(Region))
        if end is None:
            end = max((r.end for r in regions), default=0)

        p = Program()
        for region in regions:
            r_start = max(region.address, begin)
            r_end = min(region.end, end)
            if r_start >= r_end:
                continue
            offset = r_start - region.address
            size = r_end - r_start
            data = await region.read(offset, size)
            p.append(Segment(r_start, data))
        return p

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
