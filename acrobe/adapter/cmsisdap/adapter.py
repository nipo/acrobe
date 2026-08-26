"""CMSIS-DAP adapter registration + ``DAP_Info`` plumbing.

Detects via USB VID:PID; the actual HID interface is then picked
inside the transport by usage page (0xFF00). On open we read the
adapter's vendor / product / firmware version strings and its
capabilities (SWD / JTAG / SWO support, packet size, packet count)
so subsequent SWD or JTAG sub-interfaces can size their batches
correctly."""

from __future__ import annotations

import logging

from ...db import NoMatch
from ..model import Adapter, AdapterInfo, adapter_db
from . import protocol
from .transport import CmsisDapTransport


# Known CMSIS-DAP USB IDs. The CMSIS-DAP world is fragmented —
# every silicon vendor with on-board CMSIS-DAP firmware ships its
# own VID:PID, and the same firmware sometimes runs on
# generic-vendor IDs too. Add as new variants surface.
_CMSIS_DAP_INFOS = (
    AdapterInfo("lpc-link2",   vid=0x1FC9, pid=0x0090),  # NXP LPC-LINK2
    AdapterInfo("lpc11u35",    vid=0x1FC9, pid=0x00A3),  # generic LPC11U35-based
    AdapterInfo("daplink",     vid=0x0D28, pid=0x0204),  # ARM DAPLink
)


class CmsisDapAdapter(Adapter):
    """CMSIS-DAP debug adapter.

    Speaks the standard CMSIS-DAP command set over HID. The
    high-level SWD/JTAG glue is provided by the corresponding
    sub-interfaces (:mod:`.swd`, :mod:`.jtag`) — those plug the
    transport into the abstract :class:`acrobe.protocol.swd.Interface`
    and :class:`acrobe.protocol.jtag.JtagInterface` machinery."""

    def __init__(self, name: str, info: AdapterInfo, descriptor):
        super().__init__(name, info, descriptor)
        self.__transport = None
        self.vendor_name = None
        self.product_name = None
        self.fw_version = None
        self.capabilities = 0
        self.packet_size = 0
        self.packet_count = 1

    def child_hints(self):
        return ["swd", "jtag"]

    async def __ensure_open(self) -> None:
        if self.__transport is not None:
            return
        descriptor = self.descriptor
        try:
            serial_raw = descriptor.serial
        except Exception:
            serial_raw = None
        logger = logging.getLogger(self.name)

        transport = CmsisDapTransport.from_descriptor(
            descriptor.vendor_id, descriptor.product_id,
            serial=serial_raw, logger=logger)

        # DAP_Info strings + capabilities. The packet size has to be
        # known before we send anything large — read it first, then
        # update the transport so subsequent commands use the real
        # MPS. Many devices report 64; some go up to 1024.
        packet_size = await self.__info_u16(transport, protocol.INFO_PACKET_SIZE)
        if packet_size:
            transport.packet_size = packet_size
        packet_count = await self.__info_u8(transport, protocol.INFO_PACKET_COUNT) or 1

        vendor_name = await self.__info_string(transport, protocol.INFO_VENDOR_NAME)
        product_name = await self.__info_string(transport, protocol.INFO_PRODUCT_NAME)
        fw_version = await self.__info_string(transport, protocol.INFO_FW_VERSION)
        caps = await self.__info_u8(transport, protocol.INFO_CAPABILITIES)

        logger.info("CMSIS-DAP %s — %s (FW %s)",
                    vendor_name or "?", product_name or "?", fw_version or "?")
        cap_names = []
        if caps & protocol.CAP_SWD:            cap_names.append("SWD")
        if caps & protocol.CAP_JTAG:           cap_names.append("JTAG")
        if caps & protocol.CAP_SWO_UART:       cap_names.append("SWO_UART")
        if caps & protocol.CAP_SWO_MANCHESTER: cap_names.append("SWO_MANCH")
        if caps & protocol.CAP_ATOMIC_CMDS:    cap_names.append("ATOMIC")
        if caps & protocol.CAP_SWO_STREAM:     cap_names.append("SWO_STREAM")
        logger.info("CMSIS-DAP caps: 0x%02x (%s); packet_size=%d count=%d",
                    caps, ", ".join(cap_names) or "(none)",
                    packet_size, packet_count)

        self.__transport = transport
        self.vendor_name = vendor_name
        self.product_name = product_name
        self.fw_version = fw_version
        self.capabilities = caps
        self.packet_size = packet_size
        self.packet_count = packet_count

    # -- DAP_Info helpers -------------------------------------------

    @staticmethod
    async def __info_request(transport: CmsisDapTransport, info_id: int) -> bytes:
        resp = await transport.request(bytes([protocol.CMD_INFO, info_id]))
        if not resp or resp[0] != protocol.CMD_INFO:
            raise protocol.CmsisDapError(
                f"DAP_Info(0x{info_id:02x}) bad echo: "
                f"{resp[:2].hex() if resp else '(empty)'}")
        length = resp[1]
        return bytes(resp[2:2 + length])

    @classmethod
    async def __info_string(cls, transport: CmsisDapTransport,
                           info_id: int) -> str:
        data = await cls.__info_request(transport, info_id)
        return data.rstrip(b"\x00").decode("utf-8", errors="replace")

    @classmethod
    async def __info_u8(cls, transport: CmsisDapTransport,
                       info_id: int) -> int:
        data = await cls.__info_request(transport, info_id)
        return data[0] if data else 0

    @classmethod
    async def __info_u16(cls, transport: CmsisDapTransport,
                        info_id: int) -> int:
        data = await cls.__info_request(transport, info_id)
        if len(data) >= 2:
            return data[0] | (data[1] << 8)
        if data:
            return data[0]
        return 0

    # -- Sub-interface dispatch -------------------------------------

    async def child_spawn(self, name):
        await self.__ensure_open()
        if name == "swd":
            from .swd import CmsisDapSwDp
            return CmsisDapSwDp(self.__transport, self.capabilities,
                                name="swd")
        if name == "jtag":
            # JTAG slot reserved — implementation lands in a follow-up
            # commit. CMSIS-DAP supports it via DAP_JTAG_Sequence /
            # DAP_JTAG_Configure but I haven't built the interface
            # subclass yet.
            raise NoMatch("interface", name)
        raise NoMatch("interface", name)

    async def close(self):
        if self.__transport is None:
            return
        await self.__transport.close()


for _info in _CMSIS_DAP_INFOS:
    adapter_db.register(_info)(CmsisDapAdapter)
