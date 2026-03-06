"""SPI flash target.

Wraps component.spi_flash.SpiFlash as a Target with a Flash region.
"""

from . import Target
from .memory import Flash
from ..component.spi_flash import SpiFlash as SpiFlashComponent


class SpiFlashBank(Flash):
    """Flash region backed by an SpiFlash component."""

    def __init__(self, flash: SpiFlashComponent):
        erase_page_sizes = [size for size, _cmd in flash.sector_info]
        super().__init__(
            "spi-flash",
            address=0,
            size=flash.total_size,
            write_page_size=flash.page_size,
            erase_page_sizes=erase_page_sizes,
        )
        self._flash = flash

    async def read(self, offset, size):
        return await self._flash.read(offset, size)

    async def write(self, offset, data):
        await self._flash.program(offset, data)

    async def erase(self, offset, size):
        if offset == 0 and size == self.size:
            await self._flash.erase_chip()
        else:
            await self._flash.erase(offset, size)
        self.is_blank = (offset == 0 and size == self.size)


@Target.register(SpiFlashComponent)
class SpiFlashTarget(Target):
    """Target for SPI NOR flash devices."""

    def __init__(self, component: SpiFlashComponent):
        super().__init__("SPI flash " + component.name)
        self.component = component
        bank = SpiFlashBank(component)
        self.child_add(bank)

    async def erase_all(self):
        await self.component.erase_chip()
        self._force_blank()
