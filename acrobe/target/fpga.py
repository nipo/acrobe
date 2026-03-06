from . import Target
from ..component.fpga import SramFpga


@Target.register(SramFpga)
class FpgaTarget(Target):
    """Target adapter for SRAM-based FPGAs.

    No Region children. Delegates directly to SramFpga methods.
    """

    def __init__(self, component: SramFpga):
        super().__init__("SRAM config of " + component.name)
        self.component = component

    async def write(self, program, **kw):
        if kw.get("do_erase"):
            await self.component.erase()
        if len(program):
            await self.component.load(program)

    async def erase_all(self):
        await self.component.erase()

    async def verify(self, program):
        return await self.component.is_configured()
