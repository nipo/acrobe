"""FpgaTarget — SRAM-based FPGA configuration loader.

The FPGA itself doesn't expose Regions: its configuration is
applied through `SramFpga.load(bitstream)`, not via memory writes.
`FpgaLoadable.write` therefore overrides the base Loadable loop
and delegates to the component.
"""

from ..component.fpga import SramFpga, find_bitstream
from ..node import Node, Readable
from .loadable import Loadable
from .target import Target


class FpgaLoadable(Loadable):
    """Volatile-config loader for an SRAM-based FPGA."""

    def __init__(self, component: SramFpga, name="config"):
        super().__init__(name)
        self.component = component

    async def write(self, source, *, do_erase=False, do_verify=False,
                    do_start=False, update=True, assume_clean=False):
        if do_erase:
            await self.component.erase()
        payload = self.__payload(source)
        await self.component.load(payload)
        if do_verify and not await self.component.is_configured():
            self.logger.error("FPGA not configured after load")

    async def erase_all(self):
        await self.component.erase()

    async def verify(self, source) -> bool:
        return await self.component.is_configured()

    @staticmethod
    def __payload(source):
        if isinstance(source, Readable) and not source.children:
            return source
        if isinstance(source, Node):
            return find_bitstream(source)
        raise TypeError(
            f"FpgaLoadable.write expects Readable or Node, "
            f"got {type(source).__name__}")


@Target.register(SramFpga)
class FpgaTarget(Target):
    """Target wrapping an SRAM-based FPGA."""

    def __init__(self, component: SramFpga):
        super().__init__("SRAM config of " + component.name)
        self.component = component
        self.child_add(FpgaLoadable(component))
