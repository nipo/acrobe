import logging

from ...db import NoMatch
from ..model import Adapter
from .transport import FtdiTransport
from .mpsse import MpsseEngine
from .jtag import JtagMpsse


class FtdiJtagAdapter(Adapter):
    """Generic single-channel FTDI MPSSE JTAG adapter.

    Subclasses set class attributes to configure the MPSSE channel
    index (_channel) and GPIO buffer-enable pins (_gpio_oe,
    _gpio_val). Optionally set _led to an ActivityLed to blink a
    status LED while the port is active.

    The USB handle and MPSSE engine are opened lazily the first time
    the `jtag` interface is summoned, and held by the adapter for the
    session.
    """

    _channel = 0
    _gpio_oe = 0
    _gpio_val = 0
    _led = None  # Optional[ActivityLed]

    def __init__(self, name, info=None, descriptor=None):
        super().__init__(name, info, descriptor)
        self.__device = None
        self.__transport = None
        self.__engine = None

    def child_hints(self):
        return ["jtag"]

    def __resolved_gpio(self):
        gpio_oe = self._gpio_oe
        gpio_val = self._gpio_val
        if self._led is not None:
            gpio_oe |= self._led.word_mask
            gpio_val = self._led.off_bits(gpio_val)
        return gpio_oe, gpio_val

    async def __ensure_open(self):
        if self.__engine is not None:
            return
        device = self.descriptor.open()
        logger = logging.getLogger(self.name)
        transport = await FtdiTransport.from_device(
            device, interface_index=self._channel)
        engine = MpsseEngine(transport, logger)
        if self._led is not None:
            gpio_oe, gpio_val = self.__resolved_gpio()
            on_cmd, off_cmd = self._led.bracket_bytes(gpio_val, gpio_oe)
            engine.set_bracket(on_cmd, off_cmd)
        self.__device = device
        self.__transport = transport
        self.__engine = engine

    async def _ensure_engine(self):
        """Open the USB handle + MPSSE engine if needed and return it.
        Protected: subclasses that drive a different interface on the
        same channel (e.g. SWD) use this before building their own."""
        await self.__ensure_open()
        return self.__engine

    async def child_spawn(self, name):
        if name.lower() != "jtag":
            raise NoMatch("interface", name)
        engine = await self._ensure_engine()
        gpio_oe, gpio_val = self.__resolved_gpio()
        jtag = JtagMpsse(engine)
        await jtag.setup(gpio_oe=gpio_oe, gpio_val=gpio_val)
        jtag.freq_cap("hardware", 30e6)
        return jtag

    async def close(self):
        if self.__transport is not None:
            await self.__transport.close()
            self.__transport = None
        if self.__device is not None:
            self.__device.handle.close()
            self.__device = None
