from ...db import Db, NoMatch
from ..model import Adapter, AdapterInfo, adapter_db, make_adapter_name


@adapter_db.register(AdapterInfo("ftdi", vid=0x0403, manufacturer="FTDI"))
class GenericFtdiAdapter(Adapter):
    """Generic FTDI adapter for chips with default FTDI descriptors.

    Does not claim any interface or set any bitmode on open.
    Board-specific drivers register in board_db and handle
    interface setup themselves.
    """

    board_db = Db("ftdi_board")

    _adapter_info = AdapterInfo("ftdi", vid=0x0403, manufacturer="FTDI")

    def __init__(self, name, device):
        super().__init__(name)
        self.device = device

    @classmethod
    async def open(cls, descriptor):
        device = descriptor.open()
        try:
            serial = device.serial
        except Exception:
            serial = None
        name = make_adapter_name(cls._adapter_info, serial)
        return cls(name, device)

    async def child_spawn(self, name):
        return await self.board_db.acall(name, self)

    async def close(self):
        self.device.handle.close()
