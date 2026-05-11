"""TI XDS110 adapter registration and lifecycle.

Phase 1: register VID/PIDs against ``adapter_db``, claim the debug
interface on open, read XDS_VERSION (so ``acrobe info adapters``
lists the device with its firmware/hardware IDs), call XDS_CONNECT
to put the firmware in JTAG mode, and apply a default TCK speed.

Spawning ``jtag`` returns the bit-level :class:`XDS110JtagInterface`.
SWD is reserved for a follow-up commit."""

from __future__ import annotations

import logging

from ...db import NoMatch
from ..model import Adapter, AdapterInfo, adapter_db, make_adapter_name
from . import protocol
from .transport import XDS110Transport


# (vid, pid) -> (interface_index, ep_out_addr, ep_in_addr).
# The XDS110 stand-alone (1cbe:02a5) puts its debug pipe on iface 0
# with a different EP pair than the on-board variants under the TI
# 0x0451 VID; the adapter table baked into the firmware tells us
# which is which. Mirrors OpenOCD's xds110.c usb_connect() table.
_USB_TOPOLOGY = {
    (0x0451, 0xBEF3): (2, 0x02, 0x83),
    (0x0451, 0xBEF4): (2, 0x02, 0x83),
    (0x1CBE, 0x02A5): (0, 0x01, 0x81),
}

_INFOS = (
    AdapterInfo("xds110",          vid=0x0451, pid=0xBEF3),
    AdapterInfo("xds110",          vid=0x0451, pid=0xBEF4),
    AdapterInfo("xds110-standalone", vid=0x1CBE, pid=0x02A5),
)


class XDS110Adapter(Adapter):
    """TI XDS110 debug adapter.

    Currently exposes a bit-level JTAG interface that translates
    acrobe's :class:`acrobe.protocol.jtag.JtagInterface` ops into
    the firmware's state-aware vocabulary (XDS_GOTO_STATE,
    XDS_JTAG_SCAN, XDS_CYCLE_TCK). SWD lands in a later commit."""

    supported_interfaces = ["jtag"]

    def __init__(self, name: str, info: AdapterInfo, device,
                 transport: XDS110Transport, version: protocol.Version,
                 delay_count: int):
        super().__init__(name)
        self._info = info
        self._device = device
        self._transport = transport
        self.version = version
        # Track the TCK ``delay_count`` programmed at open so the
        # JTAG sub-interface can avoid a redundant XDS_SET_TCK on
        # its first flush. Updated only by sub-interfaces that
        # subsequently change the rate.
        self._delay_count = delay_count

    @classmethod
    async def open(cls, descriptor) -> "XDS110Adapter":
        device = descriptor.open()
        try:
            serial_raw = device.serial
        except Exception:
            serial_raw = None

        info = next(
            i for i in _INFOS
            if i.vid == descriptor.vendor_id
            and i.pid == descriptor.product_id)
        name = make_adapter_name(info, serial_raw)
        logger = logging.getLogger(name)

        topology = _USB_TOPOLOGY[(descriptor.vendor_id, descriptor.product_id)]
        transport = await XDS110Transport.from_device(
            device, interface_index=topology[0],
            ep_out_addr=topology[1], ep_in_addr=topology[2],
            logger=logger)

        version = await cls._get_version(transport)
        logger.info("XDS110: %s", version)
        if version.firmware < protocol.OCD_FIRMWARE_VERSION:
            logger.warning("XDS110: firmware 0x%08x is older than the "
                           "OCD-API version 0x%08x — some features will "
                           "be unavailable", version.firmware,
                           protocol.OCD_FIRMWARE_VERSION)

        # XDS_CONNECT is the mandatory firmware-side bring-up call.
        await transport.command(bytes([protocol.Opcode.XDS_CONNECT]),
                                response_payload_size=protocol.ERROR_CODE_LEN)

        # Program a sensible default TCK rate up front so the firmware
        # has a defined ``delay_count`` whether or not anyone caps the
        # frequency later. The JTAG sub-interface inherits this and
        # only re-issues XDS_SET_TCK when FreqCapper actually moves it.
        delay_count, achieved_khz = protocol.TckDelay.for_freq(
            protocol.DEFAULT_TCK_KHZ * 1000, version.firmware)
        await cls._set_tck(transport, delay_count)
        logger.debug("XDS110: TCK ~%d kHz (delay_count=0x%x)",
                     achieved_khz, delay_count)

        # nTRST is active-low in the firmware (value=0 asserts,
        # value=1 releases). Pulse it: assert → 50 TCKs → release →
        # 50 TCKs. Targets without nTRST wiring are unaffected.
        await cls._set_trst(transport, 0)
        await cls._cycle_tck(transport, 50)
        await cls._set_trst(transport, 1)
        await cls._cycle_tck(transport, 50)

        # Release nSRST too — many TI targets sit held in system
        # reset by the XDS110's nSRST line until explicitly let go.
        # Active-low like nTRST: value=1 means deasserted.
        await cls._set_srst(transport, 1)

        # Arm the JTAG engine. The JTAG sub-interface repeats this on
        # its first flush; doing it here too ensures the transport is
        # ready for any direct use before a JTAG interface is spawned.
        await transport.command(
            bytes([protocol.Opcode.CJTAG_CONNECT])
            + protocol.Bytes.pack_u32(protocol.MODE_JTAG),
            response_payload_size=protocol.ERROR_CODE_LEN)

        return cls(name, info, device, transport, version, delay_count)

    @staticmethod
    async def _set_tck(transport: XDS110Transport, delay_count: int) -> None:
        payload = (bytes([protocol.Opcode.XDS_SET_TCK])
                   + protocol.Bytes.pack_u32(delay_count))
        await transport.command(
            payload, response_payload_size=protocol.ERROR_CODE_LEN)

    @staticmethod
    async def _set_trst(transport: XDS110Transport, level: int) -> None:
        await transport.command(
            bytes([protocol.Opcode.XDS_SET_TRST, level & 0xFF]),
            response_payload_size=protocol.ERROR_CODE_LEN)

    @staticmethod
    async def _set_srst(transport: XDS110Transport, level: int) -> None:
        await transport.command(
            bytes([protocol.Opcode.XDS_SET_SRST, level & 0xFF]),
            response_payload_size=protocol.ERROR_CODE_LEN)

    @staticmethod
    async def _cycle_tck(transport: XDS110Transport, count: int) -> None:
        await transport.command(
            bytes([protocol.Opcode.XDS_CYCLE_TCK]) + protocol.Bytes.pack_u32(count),
            response_payload_size=protocol.ERROR_CODE_LEN)

    @staticmethod
    async def _get_version(transport: XDS110Transport) -> protocol.Version:
        # XDS_VERSION returns 4 bytes status + 4 bytes firmware (LE u32)
        # + 2 bytes hardware (LE u16).
        result = await transport.command(
            bytes([protocol.Opcode.XDS_VERSION]),
            response_payload_size=protocol.ERROR_CODE_LEN + 6)
        firmware = protocol.Bytes.unpack_u32(result, 0)
        hardware = protocol.Bytes.unpack_u16(result, 4)
        return protocol.Version(firmware=firmware, hardware=hardware)

    async def child_spawn(self, name):
        if name == "jtag":
            from .jtag import XDS110JtagInterface
            return XDS110JtagInterface(
                self._transport, self.version,
                initial_delay_count=self._delay_count, name="jtag")
        raise NoMatch("interface", name)

    async def close(self):
        try:
            await self._transport.command(
                bytes([protocol.Opcode.XDS_DISCONNECT]),
                response_payload_size=protocol.ERROR_CODE_LEN)
        except Exception as exc:
            self.logger.debug("XDS_DISCONNECT failed (ignored): %s", exc)
        await self._transport.close()
        self._device.handle.close()


for _info in _INFOS:
    adapter_db.register(_info)(XDS110Adapter)
