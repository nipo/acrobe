import asyncio
from pathlib import Path

from ...db import NoMatch
from ..model import Adapter, AdapterInfo, adapter_db
from ..ftdi.transport import FtdiTransport
from ..ftdi.mpsse import MpsseEngine
from ..ftdi.jtag import JtagMpsse
from ..ftdi.swd import SwdMpsse
from ...protocol.jtag import Chain, JtagInterface
from ...vfs.fs import FileNode
import acrobe.component.xilinx.formats  # noqa: F401


# Proby internal JTAG chain is on Channel B (interface index 1)
_JTAG_CHANNEL = 1

# resetn on pin 9 (GPIO H1)
_RESETN_PIN = 9


@adapter_db.register(AdapterInfo("Proby", vid=0x10eb, pid=0x0026))
class ProbyAdapter(Adapter):
    supported_interfaces = ["jtag-int", "jtag-pt", "swd-pt"]

    def __init__(self, name, device, transport, engine, jtag):
        super().__init__(name)
        self.__device = device
        self.__transport = transport
        self.__engine = engine
        self.__jtag = jtag
        self.__loaded_mode = None
        self.__transport_a = None
        self.__channel_a_lock = asyncio.Lock()

    @classmethod
    def serial_mangle(cls, serial):
        if serial is None:
            return None
        return str(int(serial.split(";")[-1]))

    @classmethod
    async def open(cls, descriptor):
        device = descriptor.open()
        try:
            serial = cls.serial_mangle(device.serial)
        except Exception:
            serial = None
        name = f"proby-{serial}" if serial else "proby"
        import logging
        logger = logging.getLogger(name)
        transport = await FtdiTransport.from_device(device, interface_index=_JTAG_CHANNEL)
        engine = MpsseEngine(transport, logger)
        jtag = JtagMpsse(engine)
        resetn_bit = 1 << _RESETN_PIN
        await jtag.setup(gpio_oe=resetn_bit, gpio_val=resetn_bit)
        return cls(name, device, transport, engine, jtag)

    async def __reprogram(self, mode):
        if self.__loaded_mode == mode:
            return

        fw_path = Path(__file__).parent / "fw" / f"{mode}.bit.gz"
        leaf = FileNode(fw_path.name, str(fw_path))
        await leaf.start()
        view = await leaf.child_summon("bitstream")

        tap = await self.__jtag.child_summon("chain", "*")
        self.logger.trace("Loading %s firmware...", mode)
        await tap.load(view)

        self.__loaded_mode = mode
            
    async def __channel_a_open(self, mode = None):
        async with self.__channel_a_lock:
            if self.__transport_a is not None:
                await self.__transport_a.close()
                self.__transport_a = None

            if mode == "mpsse":
                transport = await FtdiTransport.from_device(self.__device, interface_index=0)
                engine = MpsseEngine(transport, self.logger)
                self.__transport_a = engine
                return engine

    async def child_spawn(self, name):
        name_lower = name.lower()

        if name_lower == "jtag-int":
            self.__jtag.name = "jtag-int"
            return self.__jtag

        if name_lower == "jtag-pt":
            await self.__channel_a_open(None)
            await self.__reprogram("jtag_swd_raw")
            engine = await self.__channel_a_open("mpsse")
            jtag = JtagMpsse(engine)
            await jtag.setup(gpio_oe=0x061b, gpio_val=0x0210)
            jtag.name = "jtag-pt"
            return jtag

        if name_lower == "swd-pt":
            await self.__channel_a_open(None)
            await self.__reprogram("jtag_swd_raw")
            engine = await self.__channel_a_open("mpsse")
            swd = SwdMpsse(engine, oe_pin = 5)
            await swd.setup(gpio_oe=0x063b, gpio_val=0x0610)
            swd.name = "swd-pt"
            return swd

        raise NoMatch("interface", name)

    async def close(self):
        await self.__channel_a_open(None)
        await self.__transport.close()
        self.__device.handle.close()
