"""ARM bus-backed memory regions.

Concrete Region implementations that delegate to MemAp.mem_read/mem_write.
"""

from ...target.region import Ram, Flash


class BusRam(Ram):
    """RAM region backed by a MEM-AP."""

    def __init__(self, name, address, size, mem_ap):
        super().__init__(name, address, size)
        self.bus = mem_ap

    async def read(self, offset, size):
        return await self.bus.mem_read(self.address + offset, size)

    async def write(self, offset, data):
        await self.bus.mem_write(self.address + offset, data)


class BusFlash(Flash):
    """Flash region backed by a MEM-AP for reads.

    Erase and write are left abstract — subclasses (e.g. StubFlash)
    provide the implementation via puppet stubs or other mechanisms.
    """

    def __init__(self, name, address, size, write_page_size,
                 erase_page_sizes, mem_ap):
        super().__init__(name, address, size, write_page_size, erase_page_sizes)
        self.bus = mem_ap

    async def read(self, offset, size):
        return await self.bus.mem_read(self.address + offset, size)
