import logging

from ..model import JtagAdapter, make_adapter_name
from .transport import FtdiTransport
from .mpsse import MpsseEngine
from .jtag import JtagMpsse


class FtdiJtagAdapter(JtagAdapter):
    """Generic single-channel FTDI MPSSE JTAG adapter.

    Subclasses override class attributes to configure USB identity
    (_adapter_info), the MPSSE channel index (_channel), and GPIO
    buffer-enable pins (_gpio_oe, _gpio_val). Optionally set _led to
    an ActivityLed to blink a status LED while the port is active.
    """

    _adapter_info = None  # set by subclass
    _channel = 0
    _gpio_oe = 0
    _gpio_val = 0
    _led = None  # Optional[ActivityLed]
    _jtag_max_freq = 30e6  # FT2232H/FT4232H max JTAG clock

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

        gpio_oe = cls._gpio_oe
        gpio_val = cls._gpio_val
        if cls._led is not None:
            gpio_oe |= cls._led.word_mask
            gpio_val = cls._led.off_bits(gpio_val)

        await jtag.setup(gpio_oe=gpio_oe, gpio_val=gpio_val)

        if cls._led is not None:
            on_cmd, off_cmd = cls._led.bracket_bytes(gpio_val, gpio_oe)
            engine.set_bracket(on_cmd, off_cmd)

        return cls(name, device, transport, engine, jtag)

    async def close(self):
        await self._transport.close()
        self._device.handle.close()
