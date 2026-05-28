from ...db import Db
from ..model import Adapter, AdapterInfo, adapter_db


@adapter_db.register(AdapterInfo("ftdi", vid=0x0403, manufacturer="FTDI"))
class GenericFtdiAdapter(Adapter):
    """Generic FTDI adapter for chips with default FTDI descriptors.

    Claims no interface and sets no bitmode itself. Board-specific
    drivers register in `board_db` and are summoned by name
    (`ftdi-XXXX/icepizero/jtag`); they drive the USB handle, which
    this adapter opens lazily on first board summon.
    """

    board_db = Db("ftdi_board")

    def __init__(self, name, info=None, descriptor=None):
        super().__init__(name, info, descriptor)
        self.__device = None

    def child_hints(self):
        return list(self.board_db.registry.keys())

    async def ensure_open(self):
        """Open the USB device handle if not already open and return
        it. Boards call this before touching `self.device`."""
        if self.__device is None:
            self.__device = self.descriptor.open()
        return self.__device

    @property
    def device(self):
        assert self.__device is not None, "FTDI device not opened"
        return self.__device

    async def child_spawn(self, name):
        return await self.board_db.acall(name, self)

    async def close(self):
        if self.__device is not None:
            self.__device.handle.close()
            self.__device = None
