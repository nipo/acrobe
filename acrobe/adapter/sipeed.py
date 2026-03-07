from .model import AdapterInfo, adapter_db
from .digilent import FtdiJtagAdapter


@adapter_db.register(AdapterInfo(
    "sp", vid=0x0403, pid=0x6010, manufacturer="Sipeed"))
class SipeedAdapter(FtdiJtagAdapter):
    """Sipeed Tang Nano debug adapter.

    MCU-based MPSSE reimplementation.  Uses same VID/PID as FT2232H
    but filters on manufacturer string.
    """

    _adapter_info = AdapterInfo(
        "sp", vid=0x0403, pid=0x6010, manufacturer="Sipeed")
    _channel = 0
    _gpio_oe = 0
    _gpio_val = 0
