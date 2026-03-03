from . import Component
from ..loadable import Program


class SramFpga(Component):
    """Abstract base for SRAM-based (volatile) FPGAs."""

    async def load(self, program: Program):
        raise NotImplementedError

    async def erase(self):
        raise NotImplementedError

    async def is_configured(self) -> bool:
        raise NotImplementedError


class JtagSramFpga(SramFpga):
    """JTAG-programmable SRAM FPGA. Adds USER_IR contract."""

    USER_IR: list = []
