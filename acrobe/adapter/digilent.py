from ..db import NoMatch
from .model import Adapter, AdapterInfo, adapter_db, make_adapter_name
from .ftdi.transport import FtdiTransport
from .ftdi.mpsse import MpsseEngine
from .ftdi.jtag import JtagMpsse
from ..protocol.jtag import JtagInterface


class FtdiJtagAdapter(Adapter):
    """Simple single-channel FTDI JTAG adapter.

    Subclasses override class attributes to configure USB identity
    and GPIO buffer-enable pins.
    """

    _adapter_info = None  # set by subclass
    _channel = 0
    _gpio_oe = 0
    _gpio_val = 0

    supported_interfaces = ["jtag"]

    def __init__(self, name, device, transport, engine, jtag):
        super().__init__(name)
        self._device = device
        self._transport = transport
        self._engine = engine
        self._jtag = jtag

    @classmethod
    async def open(cls, descriptor):
        device = descriptor.open()
        try:
            serial_raw = device.serial
        except Exception:
            serial_raw = None
        serial = cls.serial_mangle(serial_raw)
        name = make_adapter_name(cls._adapter_info, serial)
        import logging
        logger = logging.getLogger(name)
        transport = await FtdiTransport.from_device(
            device, interface_index=cls._channel)
        engine = MpsseEngine(transport, logger)
        jtag = JtagMpsse(engine, logger)
        await jtag.setup(gpio_oe=cls._gpio_oe, gpio_val=cls._gpio_val)
        return cls(name, device, transport, engine, jtag)

    async def child_spawn(self, name):
        if name.lower() == "jtag":
            return JtagInterface(self._jtag, name="jtag")
        raise NoMatch("interface", name)

    async def close(self):
        await self._transport.close()
        self._device.handle.close()


@adapter_db.register(AdapterInfo(
    "hs2", vid=0x0403, pid=0x6014, manufacturer="Digilent"))
class Hs2Adapter(FtdiJtagAdapter):
    _adapter_info = AdapterInfo(
        "hs2", vid=0x0403, pid=0x6014, manufacturer="Digilent")
    _gpio_oe = 0x60E0
    _gpio_val = 0x00E0


@adapter_db.register(AdapterInfo(
    "dig", vid=0x0403, pid=0x6010, manufacturer="Digilent"))
class Smt2Adapter(FtdiJtagAdapter):
    _adapter_info = AdapterInfo(
        "dig", vid=0x0403, pid=0x6010, manufacturer="Digilent")
    _gpio_oe = 0xC0E0
    _gpio_val = 0x00C0
