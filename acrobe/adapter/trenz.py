from .model import AdapterInfo, adapter_db
from .ftdi.jtag_adapter import FtdiJtagAdapter


@adapter_db.register(AdapterInfo(
    "tei", vid=0x0403, pid=0x6010, manufacturer="Arrow"))
class Tei0003Adapter(FtdiJtagAdapter):
    _gpio_oe = 0
    _gpio_val = 0
