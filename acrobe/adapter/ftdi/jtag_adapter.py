import logging

from ...db import NoMatch
from ..model import Adapter, make_adapter_name
from .transport import FtdiTransport
from .mpsse import MpsseEngine
from .jtag import JtagMpsse
from ...protocol.jtag import JtagInterface


class FtdiJtagAdapter(Adapter):
    """Generic single-channel FTDI MPSSE JTAG adapter.

    Subclasses override class attributes to configure USB identity
    (_adapter_info), the MPSSE channel index (_channel), and GPIO
    buffer-enable pins (_gpio_oe, _gpio_val).
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
        logger = logging.getLogger(name)
        transport = await FtdiTransport.from_device(
            device, interface_index=cls._channel)
        engine = MpsseEngine(transport, logger)
        jtag = JtagMpsse(engine, logger)
        await jtag.setup(gpio_oe=cls._gpio_oe, gpio_val=cls._gpio_val)
        return cls(name, device, transport, engine, jtag)

    async def child_spawn(self, name):
        if name.lower() == "jtag":
            iface = JtagInterface(self._jtag, name="jtag")
            # FT2232H/FT4232H max JTAG clock: 30 MHz
            iface.freq_cap("hardware", 30e6)
            return iface
        raise NoMatch("interface", name)

    async def close(self):
        await self._transport.close()
        self._device.handle.close()
