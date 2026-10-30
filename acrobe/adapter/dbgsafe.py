from .model import AdapterInfo, adapter_db
from .ftdi.activity import ActivityLed
from .ftdi.jtag_adapter import FtdiJtagAdapter
from .ftdi.mpsse import SetBitsHigh
from .ftdi.spi import SpiMpsse
from .ftdi.swd import SwdMpsse


@adapter_db.register(AdapterInfo(
    "ds", vid=0x0403, pid=0x6010,
    manufacturer="Diaxen", product="DbgSafe 1.0"))
class DbgSafeAdapter(FtdiJtagAdapter):
    """Diaxen DbgSafe — FT2232H whose port A drives either JTAG, SWD
    or SPI. In SWD mode, TDI/TDO go through an external buffer whose
    output enable is on bit 12 (active-low: pin LOW = host drives
    SWDIO). SPI uses the JTAG pinout with chip select on TMS. Target
    nRESET on bit 9 is held released, activity LED is on bit 15."""

    _gpio_oe = 0x9a3b
    _gpio_val = 0x0a20
    _led = ActivityLed(pin=15)

    def child_hints(self):
        return ["jtag", "swd", "spi"]

    async def child_spawn(self, name):
        if name.lower() == "swd":
            engine = await self._ensure_engine()
            iface = SwdMpsse(engine, oen_pin=12)
            await iface.setup(gpio_oe=0x9a33, gpio_val=0x0a00)
            iface.freq_cap("hardware", 30e6)
            return iface
        if name.lower() == "spi":
            engine = await self._ensure_engine()
            iface = SpiMpsse(engine, cs_pin=3,
                             gpio_oe=0x3b, gpio_val=0x20)
            await engine.post(SetBitsHigh(0x0a, 0x9a))
            iface.freq_cap("hardware", 30e6)
            return iface
        return await super().child_spawn(name)
