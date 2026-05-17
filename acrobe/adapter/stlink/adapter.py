"""ST-Link adapter registration and child-spawn entry point.

Phase 1: registers known PIDs against ``adapter_db`` and reads
GET_VERSION on open so ``acrobe info adapters`` lists the device
with its protocol versions. Spawning ``jtag`` / ``swd`` raises
``NoMatch`` for now — phase 2 plugs in :class:`StLinkJtagDp` /
:class:`StLinkSwDp`.
"""

from __future__ import annotations

import logging

from ...db import NoMatch
from ..model import Adapter, AdapterInfo, adapter_db, make_adapter_name
from .transport import StLinkTransport
from . import protocol


# Known ST-Link USB IDs (VID 0x0483 = STMicroelectronics).
_STLINK_INFOS = (
    AdapterInfo("stlink-v2",   vid=0x0483, pid=0x3748),
    AdapterInfo("stlink-v2-1", vid=0x0483, pid=0x374B),
    AdapterInfo("stlink-v2-1", vid=0x0483, pid=0x3752),  # v2.1 NRND
    AdapterInfo("stlink-v3",   vid=0x0483, pid=0x374D),  # v3 mini
    AdapterInfo("stlink-v3",   vid=0x0483, pid=0x374E),  # v3
    AdapterInfo("stlink-v3",   vid=0x0483, pid=0x374F),  # v3
    AdapterInfo("stlink-v3",   vid=0x0483, pid=0x3753),  # v3 modular
    AdapterInfo("stlink-v3",   vid=0x0483, pid=0x3754),  # v3 box
    AdapterInfo("stlink-v3",   vid=0x0483, pid=0x3755),
    AdapterInfo("stlink-v3",   vid=0x0483, pid=0x3757),
)


class StLinkAdapter(Adapter):
    """ST-Link debug adapter.

    Exposes JTAG and SWD interfaces backed by ST-Link's high-level
    USB protocol — DP/AP read/write transactions go directly through
    ``StLinkJtagDp`` / ``StLinkSwDp``, bypassing acrobe's bit-level
    JTAG / SWD layers entirely. (ST-Link doesn't expose bit-bang
    JTAG, so reuse of those layers wouldn't help anyway.)"""

    supported_interfaces = ["jtag", "swd"]

    def __init__(self, name: str, info: AdapterInfo, device,
                 transport: StLinkTransport,
                 version: protocol.StLinkVersion):
        super().__init__(name)
        self.__info = info
        self.__device = device
        self.__transport = transport
        self.version = version

    @classmethod
    async def open(cls, descriptor) -> "StLinkAdapter":
        device = descriptor.open()
        try:
            serial_raw = device.serial
        except Exception:
            serial_raw = None

        # Pick the registered AdapterInfo whose VID+PID match this
        # descriptor — used for the friendly adapter name.
        info = next(
            i for i in _STLINK_INFOS
            if i.vid == descriptor.vendor_id
            and i.pid == descriptor.product_id)
        name = make_adapter_name(info, serial_raw)
        logger = logging.getLogger(name)

        transport = await StLinkTransport.from_device(
            device, interface_index=0, logger=logger)

        # Make sure we're not stuck in DFU bootloader mode (otherwise
        # debug commands will be NACKed).
        try:
            mode = await transport.get_current_mode()
            if mode == protocol.MODE_DFU:
                logger.info("Adapter in DFU mode, requesting exit")
                await transport.dfu_exit()
        except Exception as exc:
            logger.warning("Mode probe failed (ignored): %s", exc)

        version = await transport.get_version()
        logger.info("ST-Link version: %s", version)

        return cls(name, info, device, transport, version)

    async def child_spawn(self, name):
        from .dp import StLinkJtagDp, StLinkSwDp

        if name == "jtag":
            return StLinkJtagDp(self.__transport)
        if name == "swd":
            return StLinkSwDp(self.__transport)
        raise NoMatch("interface", name)

    async def close(self):
        await self.__transport.close()
        self.__device.handle.close()


for _info in _STLINK_INFOS:
    adapter_db.register(_info)(StLinkAdapter)
