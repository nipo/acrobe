from ..db import NoMatch
from .model import AdapterInfo, adapter_db
from .ftdi.jtag import JtagMpsse
from .ftdi.jtag_adapter import FtdiJtagAdapter
from .ftdi.swd import SwdMpsse


_HD = AdapterInfo(
    "hd", vid=0x0403, pid=0x6011,
    manufacturer="Nipo", product="Hub Debug")


@adapter_db.register(_HD)
class HdAdapter(FtdiJtagAdapter):
    """Nipo Hub Debug — single-channel FT2232H-class adapter that
    re-purposes the same MPSSE channel for either JTAG or SWD,
    distinguished by the GPIO setup applied at child_spawn time. The
    SWD variant routes TDI/TDO through an external buffer whose
    output enable is on bit 6 (active-low: pin LOW = host drives
    SWDIO)."""

    _adapter_info = _HD

    supported_interfaces = ["jtag", "swd"]

    async def child_spawn(self, name):
        if name == "jtag":
            jtag = JtagMpsse(self._engine)
            await jtag.setup(gpio_oe=0xeb, gpio_val=0x20)
            jtag.freq_cap("hardware", 30e6)
            return jtag
        if name == "swd":
            iface = SwdMpsse(self._engine, oen_pin=6)
            # gpio_oe / gpio_val carry only the *extra* board pins;
            # SwdMpsse adds TCK/TDI/TMS + the OE pin itself on top.
            await iface.setup(gpio_oe=0xe3, gpio_val=0xc0)
#            iface.freq_cap("hardware", 30e6)
            return iface
        raise NoMatch("interface", name)
