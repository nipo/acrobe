"""PICOBOOT adapter — RP2040 in BOOTSEL mode.

The "adapter" here is the chip itself, viewed through its
USB-side ROM bootloader. The `PicobootAdapter` opens the
:class:`PicobootUsbTransport` and exposes a single ``picoboot``
interface child whose Node form is the bridge into the
target-side puppet / target framework.

Discovery shape — VID 0x2e8a is Raspberry Pi; PID 0x0003 is the
RP2040 BOOTSEL bootloader. The newer RP2350 BOOTSEL is PID
0x000f and would register the same way once we wire it.
"""

from __future__ import annotations

import logging

from ...db import NoMatch
from ...component.raspberry.picoboot_transport import (
    PicobootUsbTransport, USB_VID_RPI, USB_PID_RP2040_BOOTSEL,
)
from ..model import Adapter, AdapterInfo, adapter_db


_INFOS = (
    AdapterInfo("rp2040-bootsel",
                vid=USB_VID_RPI,
                pid=USB_PID_RP2040_BOOTSEL),
)


class PicobootAdapter(Adapter):
    """RP2040 BOOTSEL-mode bootloader as an adapter.

    Unlike SWD/JTAG adapters, PICOBOOT is the target's own ROM
    bootloader speaking a vendor USB protocol — there is no
    intermediate debug interface. The "picoboot" child is a
    component Node that holds the transport and is what the
    RP2040 Target probe builds a PicobootPuppet against.
    """

    def __init__(self, name: str, info: AdapterInfo, descriptor):
        super().__init__(name, info, descriptor)
        self.__device = None
        self.__transport = None

    def child_hints(self):
        return ["picoboot"]

    async def __ensure_open(self) -> None:
        if self.__transport is not None:
            return
        device = self.descriptor.open()
        logger = logging.getLogger(self.name)
        self.__transport = await PicobootUsbTransport.from_device(
            device, logger=logger)
        self.__device = device

    async def child_spawn(self, name):
        await self.__ensure_open()
        if name == "picoboot":
            from ...component.raspberry.picoboot import Picoboot
            return Picoboot(self.__transport, name="picoboot")
        raise NoMatch("interface", name)

    async def close(self):
        if self.__transport is None:
            return
        await self.__transport.close()
        self.__device.handle.close()


for _info in _INFOS:
    adapter_db.register(_info)(PicobootAdapter)
