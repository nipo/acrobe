from ..db import NoMatch
from .model import AdapterInfo, adapter_db
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

    _gpio_oe = 0xeb
    _gpio_val = 0x20

    def child_hints(self):
        return ["jtag", "swd"]

    async def child_spawn(self, name):
        if name == "swd":
            engine = await self._ensure_engine()
            iface = SwdMpsse(engine, oen_pin=6)
            await iface.setup(gpio_oe=0xe3, gpio_val=0xc0)
            return iface
        return await super().child_spawn(name)
