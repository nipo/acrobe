from ..node import Node
from ..db import Db, NoMatch
from ..loadable import Program


class SramFpga(Node):
    """Abstract base for SRAM-based (volatile) FPGAs."""

    application_db = Db("SramFpga application")

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.application_db = Db(f"{cls.__name__} application")

    async def child_spawn(self, name):
        for cls in type(self).__mro__:
            if not (isinstance(cls, type) and issubclass(cls, SramFpga)):
                continue
            try:
                return await cls.application_db.acall(name, self)
            except NoMatch:
                continue
        raise NoMatch("child", name)

    async def load(self, program: Program):
        raise NotImplementedError

    async def erase(self):
        raise NotImplementedError

    async def is_configured(self) -> bool:
        raise NotImplementedError


class JtagSramFpga(SramFpga):
    """JTAG-programmable SRAM FPGA. Adds USER_IR contract."""

    USER_IR: list = []
