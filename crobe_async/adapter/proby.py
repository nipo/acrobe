from ..db import NoMatch
from .model import Adapter, AdapterInfo, adapter_db
from .ftdi.transport import FtdiTransport
from .ftdi.mpsse import MpsseEngine
from .ftdi.jtag import JtagMpsse


# Proby internal JTAG chain is on Channel B (interface index 1)
_JTAG_CHANNEL = 1

# resetn on pin 9 (GPIO H1)
_RESETN_PIN = 9


@adapter_db.register(AdapterInfo(0x10eb, 0x0026, "Proby", ["jtag"]))
class ProbyAdapter(Adapter):
    supported_interfaces = ["jtag"]

    def __init__(self, name, transport, engine, jtag):
        super().__init__(name)
        self._transport = transport
        self._engine = engine
        self._jtag = jtag

    @classmethod
    async def open(cls, descriptor):
        device = descriptor.open()
        transport = await FtdiTransport.from_device(device, interface_index=_JTAG_CHANNEL)
        engine = MpsseEngine(transport)
        jtag = JtagMpsse(engine)
        resetn_bit = 1 << _RESETN_PIN
        await jtag.setup(gpio_oe=resetn_bit, gpio_val=resetn_bit)
        return cls("Proby", transport, engine, jtag)

    async def child_spawn(self, name):
        if name.lower() == "jtag":
            return self._jtag
        raise NoMatch("interface", name)

    async def close(self):
        await self._transport.close()
