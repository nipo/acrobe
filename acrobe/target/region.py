"""Memory region model.

Region is a Node living under a Loadable. Subclasses (Ram, Flash,
Eeprom) describe the access semantics of one contiguous span of
target memory.

Loadable.write drives programming through a two-step protocol per
region:

  * `plan_update(region_map)` — async generator. Prepares the
    region (e.g. issues per-sector erase on Flash) and yields
    (offset, data) tuples ready for the per-page write.
  * `write(offset, data)` — applies one prepared page.

Standalone callers that want "update this address with these bytes"
use `update(offset, data)` instead; it bundles erase+write per page
and defaults to plain write on regions that need no preparation.

Regions that physically can't be reprogrammed raise
`NotUpdatable` from `update` / `plan_update`.
"""

from ..db import Db, NoMatch
from ..node import Node


class NotUpdatable(Exception):
    """Raised by a Region whose contents cannot be reprogrammed
    (one-time-programmable fuses, OTP, etc.)."""


class Region(Node):
    """Contiguous memory region.

    Subclasses implement `read`, `write`, and (where applicable)
    `erase`. Default `update` is plain `write` — suitable for Ram
    and Eeprom-like regions where no preparation is needed.
    """

    def __init__(self, name, address, size):
        super().__init__(name)
        self.address = address
        self.size = size

    @property
    def end(self):
        return self.address + self.size

    def contains(self, addr):
        return self.address <= addr < self.end

    async def read(self, offset, size):
        raise NotImplementedError

    async def write(self, offset, data):
        raise NotImplementedError

    async def erase(self, offset, size):
        pass

    async def plan_update(self, region_map):
        """Prepare the region for programming from `region_map`
        (a MemoryMap clipped to this region's range) and yield
        (offset, data) tuples ready for `self.write`.

        Default: yield each chunk unchanged, no preparation. Flash
        subclasses override to erase sectors and page-align data.
        """
        for addr, data in region_map:
            yield addr - self.address, data

    def programming_total(self, region_map) -> int:
        """Total bytes `plan_update` will hand to `write` for
        `region_map`. Used by `Loadable.write` to size progress
        bars. Default = `region_map.size`; Flash overrides to add
        the page-alignment fill."""
        return region_map.size

    async def update(self, offset, data):
        """Apply a single planned page to the region.

        Default implementation is just `write` — appropriate for
        Ram and Eeprom. Flash subclasses inherit this default
        because the erase has already been issued by `plan_update`.
        Regions that cannot be reprogrammed override to raise
        `NotUpdatable`.
        """
        await self.write(offset, data)

    def __lt__(self, other):
        return self.address < other.address

    def __repr__(self):
        return (f"<{self.__class__.__name__} '{self._name}' "
                f"0x{self.address:08x}-0x{self.end:08x}>")


class Ram(Region):
    """Volatile read/write memory.

    Doubles as the anchor point for on-demand client nodes that
    need bounded RAM access: SEGGER RTT, ITM-in-RAM logs, future
    memory-mapped peripheral drivers spawned via APB/AHB pseudo-
    RAM regions. Clients register against `Ram.db` keyed on the
    name the path resolves with (e.g. `"rtt"`); `child_spawn`
    instantiates them with `self` as the parent so they inherit
    address/size bounds for scanning and a working `bus` ref."""

    db: Db = Db("Ram client")

    async def child_spawn(self, name):
        try:
            cls = self.db.get(name)[0]
        except NoMatch:
            raise NoMatch("child", name)
        return cls(self)


class Flash(Region):
    """Flash memory with erase-before-write semantics.

    `erased_value`     — byte value after erase (typically 0xff).
    `write_page_size`  — maximum bytes per write command.
    `erase_page_sizes` — available erase granularities, ascending.

    `is_blank` tracks whether the region is known to be fully erased.
    It is updated by `erase_all` (via the owning Loadable) and by
    full-region erase; partial erases do not change it.
    """

    erased_value = 0xff

    def __init__(self, name, address, size, write_page_size, erase_page_sizes):
        super().__init__(name, address, size)
        self.write_page_size = write_page_size
        self.erase_page_sizes = sorted(erase_page_sizes)
        self.is_blank = False

    async def erase(self, offset, size):
        raise NotImplementedError

    async def plan_update(self, region_map):
        if not self.is_blank:
            await self._erase_for(region_map)
        paged = region_map.paged(self.write_page_size,
                                 fill=bytes([self.erased_value]))
        page = self.write_page_size
        for addr, data in paged:
            offset = addr - self.address
            for o in range(0, len(data), page):
                yield offset + o, data[o:o + page]

    def programming_total(self, region_map) -> int:
        return region_map.paged(
            self.write_page_size,
            fill=bytes([self.erased_value])).size

    async def _erase_for(self, region_map):
        """Issue per-erase-page erases covering every chunk in
        `region_map`. Aligns to the smallest erase granularity the
        flash exposes; dedupes pages touched by multiple chunks."""
        erase_page = self.erase_page_sizes[0]
        pages: set[int] = set()
        for addr, data in region_map:
            offset = addr - self.address
            start = offset & ~(erase_page - 1)
            end = ((offset + len(data) + erase_page - 1)
                   & ~(erase_page - 1))
            for p in range(start, end, erase_page):
                pages.add(p)
        for p in sorted(pages):
            await self.erase(p, erase_page)

    def __repr__(self):
        return (f"<{self.__class__.__name__} '{self._name}' "
                f"0x{self.address:08x}-0x{self.end:08x} "
                f"wp={self.write_page_size} ep={self.erase_page_sizes}>")


class Eeprom(Region):
    """Byte-addressable non-volatile memory. No explicit erase needed."""

    def __init__(self, name, address, size, write_page_size):
        super().__init__(name, address, size)
        self.write_page_size = write_page_size

    @property
    def is_blank(self):
        return False
