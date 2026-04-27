from . import Target
from ..node import Node, Readable
from ..component.fpga import SramFpga, find_bitstream


@Target.register(SramFpga)
class FpgaTarget(Target):
    """Target adapter for SRAM-based FPGAs.

    No Region children. Delegates directly to SramFpga methods.
    `write(node)` finds a bitstream payload (Readable) inside the
    node and passes it to component.load.
    """

    def __init__(self, component: SramFpga):
        super().__init__("SRAM config of " + component.name)
        self.component = component

    async def write(self, source, **kw):
        if kw.get("do_erase"):
            await self.component.erase()
        if isinstance(source, Readable) and not source._children:
            payload = source
        elif isinstance(source, Node):
            payload = find_bitstream(source)
        else:
            raise TypeError(
                f"FpgaTarget.write expects Readable or Node, "
                f"got {type(source).__name__}")
        await self.component.load(payload)

    async def erase_all(self):
        await self.component.erase()

    async def verify(self, source):
        return await self.component.is_configured()
