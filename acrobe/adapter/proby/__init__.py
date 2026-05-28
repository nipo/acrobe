import asyncio
from pathlib import Path

from ...db import NoMatch
from ..model import Adapter, AdapterInfo, adapter_db
from ..ftdi.transport import FtdiTransport
from ..ftdi.mpsse import MpsseEngine
from ..ftdi.jtag import JtagMpsse
from ..ftdi.swd import SwdMpsse
from ..ftdi.sync_fifo import Ft245SyncPipe
from ...protocol.jtag import Chain, JtagInterface
from ...vfs.fs import FileNode
import acrobe.component.xilinx.formats  # noqa: F401

from .transactors import (
    ProbySwdInterface,
    ProbyJtagInterface,
    ProbyI2cInterface,
)


# Proby internal JTAG chain is on Channel B (interface index 1)
_JTAG_CHANNEL = 1
_CHANNEL_A = 0

# resetn on pin 9 (GPIO H1)
_RESETN_PIN = 9

# FPGA modes — name maps to the bitstream file under ./fw/<mode>.bit.gz.
_MODE_RAW = "jtag_swd_raw"
_MODE_TRANSACTOR = "jtag_swd_i2c"


@adapter_db.register(AdapterInfo("Proby", vid=0x10eb, pid=0x0026))
class ProbyAdapter(Adapter):
    def __init__(self, name, info=None, descriptor=None):
        super().__init__(name, info, descriptor)
        self.__device = None
        self.__transport = None
        self.__engine = None
        self.__jtag = None
        self.__loaded_mode = None
        self.__transport_a = None
        # The interface currently holding channel A (None when channel
        # A is idle). Tracked so we can tear down the previous protocol
        # interface before opening the next one.
        self.__active_a_interface = None
        self.__channel_a_lock = asyncio.Lock()

    @classmethod
    def serial_mangle(cls, serial):
        if serial is None:
            return None
        return str(int(serial.split(";")[-1]))

    def child_hints(self):
        return ["jtag-int", "jtag-pt", "swd-pt", "swd", "jtag", "i2c"]

    async def __ensure_open(self):
        if self.__jtag is not None:
            return
        device = self.descriptor.open()
        # USB reset wipes residual FT2232 state from the previous
        # session. Without it, a Proby left in FT245 sync mode on
        # channel A (transactor firmware run) refuses to come up cleanly
        # on a fresh invocation — the bitmode-reset control request
        # alone doesn't clear all latched state.
        device.reset()
        transport = await FtdiTransport.from_device(
            device, interface_index=_JTAG_CHANNEL)
        engine = MpsseEngine(transport, self.logger)
        jtag = JtagMpsse(engine)
        resetn_bit = 1 << _RESETN_PIN
        await jtag.setup(gpio_oe=resetn_bit, gpio_val=resetn_bit)
        self.__device = device
        self.__transport = transport
        self.__engine = engine
        self.__jtag = jtag

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

    async def __channel_a_close(self):
        """Tear down whatever currently owns channel A."""
        if self.__active_a_interface is not None:
            try:
                await self.child_remove(self.__active_a_interface)
            except Exception:
                self.logger.warning(
                    "Failed to remove active channel-A interface",
                    exc_info=True)
            self.__active_a_interface = None
        if self.__transport_a is not None:
            await self.__transport_a.close()
            self.__transport_a = None

    async def __channel_a_open_mpsse(self):
        transport = await FtdiTransport.from_device(self.__device, interface_index=_CHANNEL_A)
        engine = MpsseEngine(transport, self.logger)
        self.__transport_a = transport
        return engine

    async def __channel_a_open_ft245_sync(self):
        transport = await FtdiTransport.from_device_ft245_sync(
            self.__device, interface_index=_CHANNEL_A)
        self.__transport_a = transport
        return transport

    async def __spawn_passthrough_jtag(self):
        async with self.__channel_a_lock:
            await self.__channel_a_close()
            await self.__reprogram(_MODE_RAW)
            engine = await self.__channel_a_open_mpsse()
            jtag = JtagMpsse(engine)
            await jtag.setup(gpio_oe=0x061b, gpio_val=0x0210)
            jtag.name = "jtag-pt"
            self.__active_a_interface = jtag
            return jtag

    async def __spawn_passthrough_swd(self):
        async with self.__channel_a_lock:
            await self.__channel_a_close()
            await self.__reprogram(_MODE_RAW)
            engine = await self.__channel_a_open_mpsse()
            swd = SwdMpsse(engine, oe_pin=5)
            await swd.setup(gpio_oe=0x063b, gpio_val=0x0610)
            swd.name = "swd-pt"
            self.__active_a_interface = swd
            return swd

    async def __spawn_transactor(self, mode_name, interface_cls):
        async with self.__channel_a_lock:
            await self.__channel_a_close()
            await self.__reprogram(_MODE_TRANSACTOR)
            transport = await self.__channel_a_open_ft245_sync()
            pipe = Ft245SyncPipe(transport, name=f"{mode_name}-fifo")
            interface = interface_cls(pipe, name=mode_name)
            self.__active_a_interface = interface
            return interface

    async def child_spawn(self, name):
        await self.__ensure_open()
        name_lower = name.lower()

        if name_lower == "jtag-int":
            self.__jtag.name = "jtag-int"
            return self.__jtag

        if name_lower == "jtag-pt":
            return await self.__spawn_passthrough_jtag()

        if name_lower == "swd-pt":
            return await self.__spawn_passthrough_swd()

        if name_lower == "swd":
            return await self.__spawn_transactor("swd", ProbySwdInterface)

        if name_lower == "jtag":
            return await self.__spawn_transactor("jtag", ProbyJtagInterface)

        if name_lower == "i2c":
            return await self.__spawn_transactor("i2c", ProbyI2cInterface)

        raise NoMatch("interface", name)

    async def close(self):
        if self.__jtag is None:
            return
        await self.__channel_a_close()
        await self.__transport.close()
        self.__device.handle.close()
