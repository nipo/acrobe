from ..db import NoMatch
from ..node import Node
from .ftdi.transport import FtdiTransport
from .ftdi.jtag_bitbang import JtagBitbang
from .ftdi.generic import GenericFtdiAdapter


@GenericFtdiAdapter.board_db.register("icepizero")
def _make_icepizero(adapter):
    return IcePiZero("icepizero", adapter)


class IcePiZero(Node):
    """iCEPi-zero board: ECP5 FPGA via JTAG through FT231XQ bitbang.

    Pin mapping (D0-D7): TX, RX, RTS, TDO(3), DTR, TCK(5), TMS(6), TDI(7).
    """

    TCK = 5
    TMS = 6
    TDI = 7
    TDO = 3

    def __init__(self, name, adapter):
        super().__init__(name)
        self.__adapter = adapter

    def child_hints(self) -> list[str]:
        return ["jtag"]

    async def child_spawn(self, name):
        if name.lower() == "jtag":
            device = await self.__adapter.ensure_open()
            oe_mask = (1 << self.TCK) | (1 << self.TMS) | (1 << self.TDI)
            transport = await FtdiTransport.from_device_bitbang(
                device, oe_mask=oe_mask)
            return JtagBitbang(transport,
                               tck=self.TCK, tms=self.TMS,
                               tdi=self.TDI, tdo=self.TDO,
                               name="jtag")
        raise NoMatch("interface", name)
