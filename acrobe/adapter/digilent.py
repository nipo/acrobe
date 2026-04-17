from .model import AdapterInfo, adapter_db
from .ftdi.jtag_adapter import FtdiJtagAdapter


@adapter_db.register(AdapterInfo(
    "hs2", vid=0x0403, pid=0x6014, manufacturer="Digilent"))
class Hs2Adapter(FtdiJtagAdapter):
    _adapter_info = AdapterInfo(
        "hs2", vid=0x0403, pid=0x6014, manufacturer="Digilent")
    _gpio_oe = 0x60E0
    _gpio_val = 0x00E0


@adapter_db.register(AdapterInfo(
    "dig", vid=0x0403, pid=0x6010, manufacturer="Digilent"))
class Smt2Adapter(FtdiJtagAdapter):
    _adapter_info = AdapterInfo(
        "dig", vid=0x0403, pid=0x6010, manufacturer="Digilent")
    _gpio_oe = 0xC0E0
    _gpio_val = 0x00C0
