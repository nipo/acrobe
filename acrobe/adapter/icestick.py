"""Lattice iCEstick evaluation board.

An FT2232H whose port A is the iCE40's configuration bus — shared
with the SPI flash the FPGA boots from — and whose port B goes
straight to FPGA pins.

Port A carries three masters: the FTDI, the flash, and the FPGA
itself once it starts booting. Every port-A datapath therefore holds
CRESET asserted for as long as it drives the pins, and releases it on
teardown, which is also what makes the FPGA boot from a flash image
just written.
"""

from ..db import NoMatch
from .ftdi.jtag import JtagMpsse
from .ftdi.mpsse import MpsseEngine
from .ftdi.spi import SpiMpsse
from .ftdi.spi_bitbang import SpiBitbang
from .ftdi.transport import FtdiTransport
from .model import Adapter, AdapterInfo, adapter_db
from ..lifecycle import cancel_shutdown, on_shutdown


class IceStickFlashSpi(SpiMpsse):
    """Port-A SPI master on the shared configuration bus.

    Holds CRESET asserted the whole time it is open, so the FPGA
    cannot boot and contend for the bus. Stopping releases CRESET and
    lets go of the bus, which starts the FPGA on whatever the flash
    now holds.
    """

    def __init__(self, engine, *, cs_pin, creset_pin, name="spi"):
        self.__creset_mask = 1 << creset_pin
        super().__init__(engine, cs_pin=cs_pin,
                         gpio_oe=self.__creset_mask, gpio_val=0,
                         name=name)

    async def stop(self):
        await self.gpio_set(self.__creset_mask, self.__creset_mask)
        await self.gpio_oe_set(0xFF, self.__creset_mask)


@adapter_db.register(AdapterInfo(
    "icestick", vid=0x0403, pid=0x6010,
    manufacturer="Lattice", product="Lattice FTUSB Interface Cable"))
class IceStickAdapter(Adapter):
    """iCEstick: FT2232H + SPI flash + iCE40HX1K.

    Children, two per physical port:

    * ``spi`` — port A, the configuration flash bus (MPSSE).
    * ``ice40`` — port A, slave-serial configuration of the FPGA
      (synchronous bitbang).
    * ``jtag-io`` / ``spi-io`` — port B, the pin header wired to FPGA
      I/Os, driving whatever the loaded design implements.

    The two datapaths of a port are mutually exclusive: summoning one
    tears the other down. The two ports are independent.
    """

    # Port A (ADBUS).
    A_SCK = 0
    A_MOSI = 1
    A_MISO = 2
    A_CS = 4
    A_CDONE = 6
    A_CRESET = 7

    # Port B (BDBUS): MPSSE pinout, first spare GPIO used as chip
    # select for the SPI datapath.
    B_CS = 4

    CHANNEL_A = 0
    CHANNEL_B = 1

    MPSSE_MAX_FREQ = 30e6

    def __init__(self, name, info=None, descriptor=None):
        super().__init__(name, info, descriptor)
        self.__device = None
        # Per channel: (child name, node, closeable holding the channel).
        self.__channels = {}

    def child_hints(self):
        return ["spi", "ice40", "jtag-io", "spi-io"]

    async def child_spawn(self, name):
        name = name.lower()
        if name == "spi":
            return await self.__spawn_flash_spi()
        if name == "ice40":
            return await self.__spawn_ice40()
        if name == "jtag-io":
            return await self.__spawn_io_jtag()
        if name == "spi-io":
            return await self.__spawn_io_spi()
        raise NoMatch("interface", name)

    async def close(self):
        for channel in list(self.__channels):
            await self.__channel_release(channel)
        if self.__device is not None:
            cancel_shutdown(self.close)
            self.__device.handle.close()
            self.__device = None

    # --- Port A ---

    async def __spawn_flash_spi(self):
        engine = await self.__mpsse_engine(self.CHANNEL_A)
        iface = IceStickFlashSpi(engine, cs_pin=self.A_CS,
                                 creset_pin=self.A_CRESET, name="spi")
        iface.freq_cap("hardware", self.MPSSE_MAX_FREQ)
        self.__claim(self.CHANNEL_A, iface)
        return iface

    async def __spawn_ice40(self):
        from ..component.lattice.ice40 import Ice40SlaveSerial

        device = await self.__ensure_device()
        await self.__channel_release(self.CHANNEL_A)
        # Configuration data goes out on the pin MPSSE calls MISO:
        # the FPGA's data input hangs off the flash's output net, so
        # the FTDI has to drive that net, which only bitbang can do.
        shifter = await SpiBitbang.open(
            device, interface_index=self.CHANNEL_A,
            sck=self.A_SCK, mosi=self.A_MISO, csn=self.A_CS,
            outputs={Ice40SlaveSerial.CRESET: self.A_CRESET},
            inputs={Ice40SlaveSerial.CDONE: self.A_CDONE},
            name=f"{self.name}.ice40")
        fpga = Ice40SlaveSerial(shifter, name="ice40")
        self.__claim(self.CHANNEL_A, fpga, closeable=shifter)
        return fpga

    # --- Port B ---

    async def __spawn_io_jtag(self):
        engine = await self.__mpsse_engine(self.CHANNEL_B)
        jtag = JtagMpsse(engine)
        jtag.name = "jtag-io"
        await jtag.setup()
        jtag.freq_cap("hardware", self.MPSSE_MAX_FREQ)
        self.__claim(self.CHANNEL_B, jtag)
        return jtag

    async def __spawn_io_spi(self):
        engine = await self.__mpsse_engine(self.CHANNEL_B)
        iface = SpiMpsse(engine, cs_pin=self.B_CS, name="spi-io")
        iface.freq_cap("hardware", self.MPSSE_MAX_FREQ)
        self.__claim(self.CHANNEL_B, iface)
        return iface

    # --- Channel bookkeeping ---

    async def __ensure_device(self):
        if self.__device is None:
            self.__device = self.descriptor.open()
            on_shutdown(self.close)
        return self.__device

    async def __mpsse_engine(self, channel):
        device = await self.__ensure_device()
        await self.__channel_release(channel)
        transport = await FtdiTransport.from_device(
            device, interface_index=channel)
        self.__channels[channel] = (None, None, transport)
        return MpsseEngine(transport, self.logger)

    def __claim(self, channel, node, closeable=None):
        """Record which node holds a channel. `closeable` defaults to
        the transport opened by `__mpsse_engine`."""
        _name, _node, held = self.__channels.get(channel, (None, None, None))
        self.__channels[channel] = (node.name, node,
                                    closeable if closeable is not None else held)

    async def __channel_release(self, channel):
        """Tear down whatever holds `channel`, so the next datapath
        can take the pins."""
        entry = self.__channels.pop(channel, None)
        if entry is None:
            return
        name, node, closeable = entry
        if node is not None:
            self.logger.note("Releasing %s to free channel %d", name, channel)
            if node.parent is self:
                await self.child_remove(node)
            else:
                await node.stop_tree()
        if closeable is not None:
            await closeable.close()
