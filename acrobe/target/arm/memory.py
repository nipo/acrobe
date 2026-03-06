"""ARM StubFlash: flash programmed via puppet binary stubs."""

import binascii

from ...component.arm.memory import BusFlash


class StubFlash(BusFlash):
    """Flash programmed via puppet binary stubs.

    Subclasses must set RANGE_ERASE and PAGE_WRITE class attributes
    with compiled machine code for erase and write operations.

    Supports double-buffered pipelined writes for overlapping
    data upload with flash programming.

    RANGE_ERASE stub signature: erase(address, size, page_size)
    PAGE_WRITE stub signature: write(address, buffer, size)
    """

    RANGE_ERASE: bytes  # subclass sets
    PAGE_WRITE: bytes   # subclass sets

    def __init__(self, name, address, size, write_page_size,
                 erase_page_sizes, mem_ap, puppet):
        super().__init__(name, address, size, write_page_size,
                         erase_page_sizes, mem_ap)
        self._puppet = puppet

    async def erase(self, offset, size):
        await self.puppet_erase(self.address + offset, size)
        if offset == 0 and size == self.size:
            self.is_blank = True

    async def write(self, offset, data):
        if not self.is_blank:
            await self.puppet_erase(self.address + offset, len(data))
        await self.puppet_write({self.address + offset: data})
        self.is_blank = False

    async def puppet_erase(self, address, size):
        """Erase a range using the RANGE_ERASE stub."""
        code = self._puppet.stub(self.RANGE_ERASE)
        try:
            timeout = 0.2 + 0.1 * size / self.erase_page_sizes[0]
            await code.call(address, size, self.erase_page_sizes[0],
                            timeout=timeout)
        finally:
            code.cleanup()

    async def puppet_write(self, pages):
        """Write pages using PAGE_WRITE stub with double-buffered pipelining.

        Args:
            pages: dict mapping address -> data bytes
        """
        if not pages:
            return

        code = self._puppet.stub(self.PAGE_WRITE)
        write_buffer = None
        other_buffer = None
        try:
            write_buffer = self._puppet.allocate(
                self.write_page_size, self.write_page_size)
            try:
                other_buffer = self._puppet.allocate(
                    self.write_page_size, self.write_page_size)
            except ValueError:
                other_buffer = None

            running = False
            with self.progress("Writing", len(pages), "pages") as p:
                for address, data in sorted(pages.items()):
                    await write_buffer.write(data)

                    if running:
                        await code.wait(timeout=1.0)
                        running = False

                    await code.prepare(address, write_buffer.address,
                                       self.write_page_size)
                    await code.run()
                    running = True

                    if other_buffer:
                        other_buffer, write_buffer = write_buffer, other_buffer
                    else:
                        await code.wait(timeout=1.0)
                        running = False

                    p.advance()

                if running:
                    await code.wait(timeout=1.0)
        finally:
            if write_buffer:
                self._puppet.free(write_buffer)
            if other_buffer:
                self._puppet.free(other_buffer)
            code.cleanup()

    async def puppet_update(self, pages):
        """CRC-based selective update. Only writes pages that differ.

        Args:
            pages: dict mapping address -> data bytes
        """
        if not pages:
            return

        to_scan = [(addr, len(data)) for addr, data in pages.items()]
        crcs = await self._puppet.crc32_many(to_scan)

        matching = set()
        for addr, data in pages.items():
            host_crc = binascii.crc32(data) & 0xffffffff
            if host_crc == crcs.get(addr, ~host_crc):
                matching.add(addr)

        self.logger.trace("Updating: %d/%d pages already match",
                          len(matching), len(pages))

        for addr in matching:
            del pages[addr]

        await self.puppet_write(pages)
