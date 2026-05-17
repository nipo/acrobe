"""J-Link adapter registration + ``info adapters`` plumbing.

Phase 1: USB open, GET_VERSION, GET_CAPS, log them. ``child_spawn``
raises NoMatch — phase 2 wires up the JTAG bit-bang interface.
"""

from __future__ import annotations

import logging

from ...db import NoMatch
from ..model import Adapter, AdapterInfo, adapter_db, make_adapter_name
from . import protocol
from .transport import JLinkTransport


# Known SEGGER J-Link USB IDs. The list isn't exhaustive — add PIDs
# as new variants surface.
_JLINK_INFOS = (
    AdapterInfo("jlink",     vid=0x1366, pid=0x0101),  # original
    AdapterInfo("jlink-plus", vid=0x1366, pid=0x0102),
    AdapterInfo("jlink-ultra", vid=0x1366, pid=0x0103),
    AdapterInfo("jlink-lite", vid=0x1366, pid=0x0104),
    AdapterInfo("jlink-ob",  vid=0x1366, pid=0x0105),
    AdapterInfo("jlink-ob",  vid=0x1366, pid=0x0107),
    AdapterInfo("jlink-ob",  vid=0x1366, pid=0x0108),
    AdapterInfo("jlink-ob",  vid=0x1366, pid=0x1015),  # K22-SiFive / EFM32 STK
    AdapterInfo("jlink-ob",  vid=0x1366, pid=0x1016),
    AdapterInfo("jlink-ob",  vid=0x1366, pid=0x1017),
    AdapterInfo("jlink-ob",  vid=0x1366, pid=0x1020),
    AdapterInfo("jlink-ob",  vid=0x1366, pid=0x1024),  # Renesas
)


class JLinkAdapter(Adapter):
    """SEGGER J-Link debug adapter.

    Real bit-bang JTAG and SWD via the JTAG_IO_V3 / SWD_IO commands —
    so the existing :class:`JtagInterface` machinery and slice 1-6's
    Dp/Ap/CoreSight stack run unchanged on top.

    Phase 1 doesn't yet implement child_spawn; it surfaces the
    adapter and its capabilities in ``info adapters``."""

    supported_interfaces = ["jtag", "swd"]

    def __init__(self, name: str, info: AdapterInfo, device,
                 transport: JLinkTransport, firmware_version: str,
                 hardware_version: tuple[int, int, int, int],
                 caps: bytes, register_handle: int | None):
        super().__init__(name)
        self.__info = info
        self.__device = device
        self.__transport = transport
        self.firmware_version = firmware_version
        self.hardware_version = hardware_version
        self.caps = caps
        # Connection handle returned by CMD_REGISTER, if we claimed
        # one. Closed back out via CMD_REGISTER(unregister) on close.
        self.__register_handle = register_handle

    @classmethod
    async def open(cls, descriptor) -> "JLinkAdapter":
        device = descriptor.open()
        try:
            serial_raw = device.serial
        except Exception:
            serial_raw = None

        info = next(
            i for i in _JLINK_INFOS
            if i.vid == descriptor.vendor_id
            and i.pid == descriptor.product_id)
        name = make_adapter_name(info, serial_raw)
        logger = logging.getLogger(name)

        transport = await JLinkTransport.from_device(device, logger=logger)

        firmware_version = await transport.get_firmware_version()
        logger.info("J-Link firmware: %s", firmware_version)

        caps = await transport.get_caps()

        # CMD_GET_HW_VERSION needs the GET_HW_VERSION capability.
        if protocol.has_cap(caps, protocol.CAP_GET_HW_VERSION):
            hardware_version = await transport.get_hardware_version()
            logger.info(
                "J-Link hardware: type=%d v%d.%d.%d",
                hardware_version[0], hardware_version[1],
                hardware_version[2], hardware_version[3])
        else:
            hardware_version = (0, 0, 0, 0)


        # JTAG_IO_V3 (with per-transaction status byte) is only
        # available on hardware major version ≥ 5; older OB
        # hardware uses V2 with a fixed-length TDO-only response.
        transport.jtag_io_v3 = hardware_version[1] >= 5

        # Claim a connection handle if the firmware supports it.
        # JLinkExe and crobe both do this unconditionally when
        # CAP_REGISTER is set — lets the firmware track concurrent
        # host connections and (on close) frees the slot promptly.
        register_handle = None
        if protocol.has_cap(caps, protocol.CAP_REGISTER):
            try:
                register_handle = await transport.register(True)
                logger.debug("J-Link registered handle=0x%04x",
                             register_handle)
            except Exception as exc:
                logger.warning("CMD_REGISTER failed: %s", exc)

        # Release target reset early — some adapters power up with
        # nRST asserted, which masks the target's TDO/SWDIO drivers.
        await transport.deassert_reset()

        # Surface target power + pin state. Particularly useful for
        # diagnosing "TDO stuck high" — Vtref=0 means the target is
        # unpowered, TRES=0 (asserted) means we're holding it in
        # reset.
        try:
            hw = await transport.get_hw_status()
            logger.info(
                "Target Vtref=%.2fV pins TCK=%d TDI=%d TDO=%d TMS=%d "
                "TRES=%d TRST=%d",
                hw["target_voltage_mv"] / 1000,
                hw["tck"], hw["tdi"], hw["tdo"],
                hw["tms"], hw["tres"], hw["trst"])
        except Exception as exc:
            logger.warning("get_hw_status failed: %s", exc)

        # Log the high-level capabilities the user is most likely to
        # care about.
        cap_names = []
        for bit, name_attr in (
            (protocol.CAP_SELECT_IF,    "SELECT_IF"),
            (protocol.CAP_SPEED_INFO,   "SPEED_INFO"),
            (protocol.CAP_READ_MEM,     "READ_MEM"),
            (protocol.CAP_WRITE_MEM,    "WRITE_MEM"),
            (protocol.CAP_SWO,          "SWO"),
            (protocol.CAP_GET_EXT_CAPS, "EXT_CAPS"),
        ):
            if protocol.has_cap(caps, bit):
                cap_names.append(name_attr)
        logger.info("J-Link caps: %s", ", ".join(cap_names) or "(none)")

        if protocol.has_cap(caps, protocol.CAP_SELECT_IF):
            try:
                avail = await transport.get_available_interfaces()
                tifs = []
                if avail & (1 << protocol.TIF_JTAG): tifs.append("JTAG")
                if avail & (1 << protocol.TIF_SWD):  tifs.append("SWD")
                if avail & (1 << protocol.TIF_BDM3): tifs.append("BDM3")
                if avail & (1 << protocol.TIF_FINE): tifs.append("FINE")
                logger.info("J-Link interfaces: 0x%08x (%s)",
                            avail, ", ".join(tifs) or "?")
            except Exception as exc:
                logger.warning("get_available_interfaces failed: %s", exc)

        return cls(name, info, device, transport,
                   firmware_version, hardware_version, caps,
                   register_handle)

    async def child_spawn(self, name):
        if name == "jtag":
            from .jtag import JtagJlink
            jtag = JtagJlink(self.__transport, name="jtag")
            await jtag.setup(freq_khz=1000)
            return jtag
        if name == "swd":
            from .swd import JLinkSwdInterface
            return JLinkSwdInterface(self.__transport, name="swd")
        raise NoMatch("interface", name)

    async def close(self):
        if self.__register_handle is not None:
            try:
                await self.__transport.register(False, self.__register_handle)
            except Exception:
                # Best-effort: a failed unregister shouldn't prevent
                # USB cleanup. The firmware times out stale handles
                # on its own.
                pass
            self.__register_handle = None
        await self.__transport.close()
        self.__device.handle.close()


for _info in _JLINK_INFOS:
    adapter_db.register(_info)(JLinkAdapter)
