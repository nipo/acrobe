"""SpiFlashTarget — SPI NOR flash chip as a Target.

One Loadable child ("main") holding one Flash region that
delegates to the underlying `SpiFlash` component for IO.
"""

from ..component.spi_flash import SpiFlash as SpiFlashComponent
from .loadable import Loadable
from .region import Flash
from .target import Target


class SpiFlashBank(Flash):
    """Flash region backed by a `SpiFlash` component."""

    def __init__(self, flash: SpiFlashComponent):
        erase_page_sizes = [size for size, _cmd in flash.sector_info]
        super().__init__(
            "spi-flash",
            address=0,
            size=flash.total_size,
            write_page_size=flash.page_size,
            erase_page_sizes=erase_page_sizes,
        )
        self.flash = flash

    async def read(self, offset, size):
        return await self.flash.mem_read(offset, size)

    async def write(self, offset, data):
        await self.flash.mem_write(offset, data)

    async def erase(self, offset, size):
        if offset == 0 and size == self.size:
            await self.flash.erase_chip()
            self.is_blank = True
        else:
            await self.flash.erase(offset, size)


class SpiFlashLoadable(Loadable):
    """Loadable wrapper using the component's chip-erase shortcut
    for full-bank erase."""

    def __init__(self, component: SpiFlashComponent, name="main"):
        super().__init__(name)
        self.component = component
        self.child_add(SpiFlashBank(component))

    async def erase_all(self):
        await self.component.erase_chip()
        for f in self.children_of_class(Flash):
            f.is_blank = True


@Target.register(SpiFlashComponent)
class SpiFlashTarget(Target):
    """Target for SPI NOR flash devices."""

    def __init__(self, component: SpiFlashComponent):
        super().__init__("SPI flash " + component.name)
        self.component = component
        self.child_add(SpiFlashLoadable(component))
