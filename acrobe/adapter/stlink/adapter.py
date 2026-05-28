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
from ..model import Adapter, AdapterInfo, adapter_db
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

    def __init__(self, name: str, info: AdapterInfo, descriptor):
        super().__init__(name, info, descriptor)
        self.__device = None
        self.__transport = None
        self.version = None

    def child_hints(self):
        return ["jtag", "swd"]

    async def __ensure_open(self) -> None:
        if self.__transport is not None:
            return
        device = self.descriptor.open()
        logger = logging.getLogger(self.name)

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

        self.__device = device
        self.__transport = transport
        self.version = version

    async def child_spawn(self, name):
        await self.__ensure_open()
        from .dp import StLinkJtagDp, StLinkSwDp

        if name == "jtag":
            return StLinkJtagDp(self.__transport)
        if name == "swd":
            return StLinkSwDp(self.__transport)
        raise NoMatch("interface", name)

    async def close(self):
        if self.__transport is None:
            return
        await self.__transport.close()
        self.__device.handle.close()


for _info in _STLINK_INFOS:
    adapter_db.register(_info)(StLinkAdapter)
