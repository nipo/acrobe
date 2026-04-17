from .model import AdapterInfo, adapter_db
from .digilent import FtdiJtagAdapter

# on-board USB Blaster 3 is an FT4232H with Altera VID.
# FT4232H has 4 channels; JTAG is channel A.
@adapter_db.register(AdapterInfo("ub3", vid=0x09fb, pid=0x6026))
class UsbBlaster3Adapter(FtdiJtagAdapter):
    _adapter_info = AdapterInfo("ub3", vid=0x09fb, pid=0x6026)
    _channel = 0
    _gpio_oe = 0x5b
    _gpio_val = 0xba
