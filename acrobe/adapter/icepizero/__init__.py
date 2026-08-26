from pathlib import Path

from ...db import NoMatch
from ...node import Node
from ...vfs.fs import FileNode
from ..ftdi.transport import FtdiTransport
from ..ftdi.jtag_bitbang import JtagBitbang
from ..ftdi.sync_fifo import Ft245SyncPipe
from ..ftdi.generic import GenericFtdiAdapter
from .spi import IcepizeroUartSpi


# ECP5 UART-SPI bridge bitstream, JTAG-loaded before the UART datapath
# is brought up.
_BRIDGE_FW = Path(__file__).parent / "fw" / "uart_spi.bin.gz"


@GenericFtdiAdapter.board_db.register("icepizero")
def _make_icepizero(adapter):
    return IcePiZero("icepizero", adapter)


class IcePiZero(Node):
    """iCEPi-zero board: ECP5 FPGA via JTAG through FT231XQ bitbang.

    Pin mapping (D0-D7): TX, RX, RTS, TDO(3), DTR, TCK(5), TMS(6), TDI(7).

    Two datapaths are exposed:

    * ``jtag`` — the raw FT231X sync-bitbang JTAG interface to the ECP5.
    * ``spi`` — JTAG-loads the UART-SPI bridge bitstream, then reopens
      the same channel as a 3 Mbaud UART carrying an HDLC/routed SPI
      transactor stream to the config flash. The two modes share one
      physical channel and are mutually exclusive.
    """

    TCK = 5
    TMS = 6
    TDI = 7
    TDO = 3

    def __init__(self, name, adapter):
        super().__init__(name)
        self.__adapter = adapter
        # The FtdiTransport currently holding the channel, if any. The
        # bitbang and UART modes are mutually exclusive, so a transport
        # is torn down before the next one opens.
        self.__transport = None

    def child_hints(self) -> list[str]:
        return ["jtag", "spi"]

    @property
    def __jtag_oe_mask(self):
        return (1 << self.TCK) | (1 << self.TMS) | (1 << self.TDI)

    async def __close_transport(self):
        if self.__transport is not None:
            await self.__transport.close()
            self.__transport = None

    async def __open_bitbang_jtag(self, device):
        await self.__close_transport()
        transport = await FtdiTransport.from_device_bitbang(
            device, oe_mask=self.__jtag_oe_mask)
        self.__transport = transport
        return JtagBitbang(transport,
                           tck=self.TCK, tms=self.TMS,
                           tdi=self.TDI, tdo=self.TDO, name="jtag")

    async def __load_bridge(self, device):
        # Ensure the ECP5 TAP subclass and the .bin.gz bitstream parser
        # are registered before summon.
        import acrobe.component.lattice.ecp5  # noqa: F401
        import acrobe.component.lattice.formats  # noqa: F401

        jtag = await self.__open_bitbang_jtag(device)
        tap = await jtag.child_summon("chain", "*")
        leaf = FileNode(_BRIDGE_FW.name, str(_BRIDGE_FW))
        await leaf.start()
        view = await leaf.child_summon("bitstream")
        self.logger.note("Loading UART-SPI bridge bitstream...")
        await tap.load(view)
        await self.__close_transport()

    async def child_spawn(self, name):
        device = await self.__adapter.ensure_open()
        name = name.lower()

        if name == "jtag":
            return await self.__open_bitbang_jtag(device)

        if name == "spi":
            # Once "spi" is matched, a failure below is a real error, not
            # a spawn decline — otherwise child_spawn's NoMatch handling
            # would swallow it and report "no spi child".
            try:
                await self.__load_bridge(device)
            except NoMatch as e:
                raise RuntimeError(
                    f"icepizero SPI bridge load failed: {e}") from e
            transport = await FtdiTransport.from_device_uart(
                device, baudrate=1_000_000, xon_xoff=True)
            self.__transport = transport
            pipe = Ft245SyncPipe(transport, name="uart")
            return IcepizeroUartSpi(pipe, name="spi")

        raise NoMatch("interface", name)
