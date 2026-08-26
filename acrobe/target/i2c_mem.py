"""I2C memory as a Target.

One Loadable child ("main") holding one Eeprom region that delegates
to the underlying `I2cMem` component. EEPROM cells are overwritten in
place, so the region has no erase phase: `Region.erase` stays the
inherited no-op and `plan_update` hands chunks straight to `write`,
which splits them per page on the wire.
"""

from ..component.i2c_mem import I2cMem as I2cMemComponent
from ..db import NoMatch
from .loadable import Loadable
from .region import Eeprom
from .target import Target


class I2cMemRegion(Eeprom):
    """Eeprom region backed by an `I2cMem` component."""

    def __init__(self, mem: I2cMemComponent, name: str = "data"):
        super().__init__(name, address=0, size=mem.size,
                         write_page_size=mem.page_size)
        self.mem = mem

    async def read(self, offset, size):
        return await self.mem.mem_read(offset, size)

    async def write(self, offset, data):
        await self.mem.mem_write(offset, data)


@Target.register(I2cMemComponent)
class I2cMemTarget(Target):
    """Target for byte-addressed I2C memories."""

    def __init__(self, component: I2cMemComponent):
        # Geometry is resolved in the component's start(); a node the
        # user attached but never started has nothing to program.
        if not component.started:
            raise NoMatch("i2c memory target", component.path)
        super().__init__("I2C memory " + component.name)
        self.component = component
        loadable = Loadable("main")
        loadable.child_add(I2cMemRegion(component))
        self.child_add(loadable)
