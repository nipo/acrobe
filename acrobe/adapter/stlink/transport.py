"""ST-Link USB transport.

ST-Link uses a SCSI-style command/response protocol over USB bulk
endpoints. Every transaction is:

  1. Host sends a 16-byte command packet on the bulk OUT endpoint
     (zero-padded if shorter).
  2. Device replies with a variable-length response on the bulk IN
     endpoint. The host must know the expected response size from
     the command type.

The transport here is generic; opcode-specific knowledge lives in
:mod:`.protocol`.
"""

from __future__ import annotations

import asyncio
import logging
import usb1
from ausb.handle import BulkOutEndpoint, BulkInEndpoint

from . import protocol


# Every ST-Link command frame is exactly 16 bytes wide. Shorter
# commands are zero-padded by the transport.
_COMMAND_BYTES = 16


class StLinkTransport:
    """Async USB transport for ST-Link debug adapters.

    Owns the bulk OUT / IN endpoints. The single ``command`` method
    is the only entry point — serialised via an ``asyncio.Lock`` so
    callers can issue concurrent coroutines without interleaving
    USB transactions."""

    def __init__(self, device, interface_index: int,
                 ep_out: BulkOutEndpoint, ep_in: BulkInEndpoint,
                 logger: logging.Logger):
        self._device = device
        self._interface = interface_index
        self._ep_out = ep_out
        self._ep_in = ep_in
        self._lock = asyncio.Lock()
        self._logger = logger

    @classmethod
    async def from_device(cls, device, *, interface_index: int = 0,
                          logger: logging.Logger | None = None
                          ) -> "StLinkTransport":
        """Claim the debug interface and discover its bulk endpoints.

        ST-Link SKUs differ in endpoint addresses (e.g. 0x01/0x81 on
        some v2s, 0x06/0x86 on v3 modular). We pick the first bulk
        OUT and bulk IN found in the chosen interface descriptor."""
        if logger is None:
            logger = logging.getLogger("stlink.transport")

        try:
            device.handle.detachKernelDriver(interface_index)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported,
                usb1.USBErrorAccess):
            pass
        device.handle.claimInterface(interface_index)

        ep_out_addr, ep_in_addr, mps = cls._find_endpoints(
            device, interface_index)
        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)

        logger.debug("ST-Link USB EP_OUT=0x%02x EP_IN=0x%02x mps=%d",
                     ep_out_addr, ep_in_addr, mps)

        # Drain any stale bytes left in the IN endpoint by a previous
        # session that died without reading a response. Without this,
        # the next command/response cycle gets out of sync and times
        # out. Short timeout — we expect the queue to be empty in the
        # common case.
        from ausb.exception import TransferTimeout
        try:
            while True:
                ep_in.read_sync(mps, timeout=20)
        except TransferTimeout:
            pass

        return cls(device, interface_index, ep_out, ep_in, logger)

    @staticmethod
    def _find_endpoints(device, interface_index: int):
        """Return (ep_out_addr, ep_in_addr, mps) for the debug
        interface — the first bulk OUT and bulk IN found in the
        active interface descriptor. ``mps`` is the smaller of the
        two endpoints' MPS, which is also the natural transfer
        granule for read."""
        config = device.descriptor[device.configuration]
        setting = config[interface_index][0]

        ep_out_addr = ep_in_addr = None
        ep_out_mps = ep_in_mps = 0
        # libusb endpoint type for bulk = 2 (per USB 2.0 spec).
        BULK = 2
        for ep in setting:
            if (ep.attributes & 0x3) != BULK:
                continue
            is_in = bool(ep.address & 0x80)
            if is_in and ep_in_addr is None:
                ep_in_addr = ep.address
                ep_in_mps = ep.max_packet_size
            elif not is_in and ep_out_addr is None:
                ep_out_addr = ep.address
                ep_out_mps = ep.max_packet_size

        if ep_out_addr is None or ep_in_addr is None:
            raise RuntimeError(
                f"ST-Link interface {interface_index} doesn't expose "
                f"the expected bulk OUT + bulk IN endpoint pair")

        return ep_out_addr, ep_in_addr, max(ep_out_mps, ep_in_mps)

    async def command(self, cmd: bytes, response_size: int) -> bytes:
        """Send a command frame and read the response.

        ``cmd`` is the variable-length command bytes (1..16 bytes);
        the transport zero-pads to the 16-byte frame. ``response_size``
        is exact — most ST-Link responses have a known fixed size."""
        if len(cmd) > _COMMAND_BYTES:
            raise ValueError(
                f"ST-Link command too long: {len(cmd)} > {_COMMAND_BYTES}")
        padded = cmd + bytes(_COMMAND_BYTES - len(cmd))
        async with self._lock:
            self._logger.protocol(
                "ST-Link cmd %s (response %d B)",
                " ".join(f"{b:02x}" for b in cmd[:4]),
                response_size)
            await self._ep_out.write(padded)
            if response_size == 0:
                return b""
            resp = await self._ep_in.read(response_size)
            self._logger.protocol(
                "ST-Link rsp %s",
                " ".join(f"{b:02x}" for b in resp[:8]))
            return resp

    async def get_version(self) -> protocol.StLinkVersion:
        """Read GET_VERSION (and GET_VERSION_EXT on v3 to fill in
        bridge / MSC fields). Returns the unified
        :class:`StLinkVersion`."""
        legacy = await self.command(
            bytes([protocol.CMD_GET_VERSION]), 6)
        version = protocol.parse_version_legacy(legacy)
        if version.stlink >= 3:
            ext = await self.command(
                bytes([protocol.CMD_GET_VERSION_EXT]), 12)
            version = protocol.parse_version_ext(ext)
        return version

    async def get_current_mode(self) -> int:
        """Read GET_CURRENT_MODE — returns one of the MODE_*
        constants in :mod:`.protocol`."""
        resp = await self.command(
            bytes([protocol.CMD_GET_CURRENT_MODE]), 2)
        return resp[0]

    async def dfu_exit(self) -> None:
        """Leave DFU bootloader mode. Idempotent on devices that
        aren't currently in DFU mode."""
        await self.command(
            bytes([protocol.CMD_DFU, protocol.DFU_EXIT]), 0)

    # -- Debug-mode entry / exit -----------------------------------

    @staticmethod
    def _check_status(resp: bytes, context: str = "") -> None:
        """Raise :class:`StLinkError` if the command status isn't OK.

        Status byte is at response[0]. Most DEBUG transactions return
        2-byte status; READ_DAP_REG returns 8 bytes (4 status + 4 data
        LE) but the first byte still carries the status code."""
        if not resp:
            return
        if resp[0] != protocol.DEBUG_ERR_OK:
            raise protocol.StLinkError(resp[0], context)

    async def enter_jtag(self) -> None:
        """Enter JTAG debug mode without asserting reset."""
        resp = await self.command(
            bytes([protocol.CMD_DEBUG,
                   protocol.DEBUG_APIV2_ENTER,
                   protocol.ENTER_JTAG_NO_RESET]), 2)
        self._check_status(resp, "enter_jtag")

    async def enter_swd(self) -> None:
        """Enter SWD debug mode without asserting reset."""
        resp = await self.command(
            bytes([protocol.CMD_DEBUG,
                   protocol.DEBUG_APIV2_ENTER,
                   protocol.ENTER_SWD_NO_RESET]), 2)
        self._check_status(resp, "enter_swd")

    async def exit_debug(self) -> None:
        """Leave debug mode. The command returns no response."""
        await self.command(
            bytes([protocol.CMD_DEBUG, protocol.DEBUG_EXIT]), 0)

    # -- DP / AP register access -----------------------------------
    #
    # READ_DAP_REG / WRITE_DAP_REG share opcodes for DP and AP. The
    # ``dap_port`` field selects: 0xFFFF for DP register access; an
    # AP index (0..255) for AP register access. Non-IDR AP reads
    # require :meth:`init_ap` first — IDR (0xFC) is special-cased
    # by the firmware.

    async def read_dap_reg(self, dap_port: int, addr: int) -> int:
        """Read a 32-bit DP or AP register. Response is 8 bytes:
        4 status + 4 LE data. Raises :class:`StLinkError` on
        non-OK status."""
        cmd = bytes([
            protocol.CMD_DEBUG,
            protocol.DEBUG_APIV2_READ_DAP_REG,
            dap_port & 0xFF, (dap_port >> 8) & 0xFF,
            addr & 0xFF, (addr >> 8) & 0xFF,
        ])
        resp = await self.command(cmd, 8)
        self._check_status(
            resp, f"read_dap_reg(port=0x{dap_port:04x}, addr=0x{addr:04x})")
        return (resp[4] | (resp[5] << 8)
                | (resp[6] << 16) | (resp[7] << 24))

    async def write_dap_reg(self, dap_port: int, addr: int, value: int) -> None:
        """Write a 32-bit DP or AP register."""
        cmd = bytes([
            protocol.CMD_DEBUG,
            protocol.DEBUG_APIV2_WRITE_DAP_REG,
            dap_port & 0xFF, (dap_port >> 8) & 0xFF,
            addr & 0xFF, (addr >> 8) & 0xFF,
            value & 0xFF, (value >> 8) & 0xFF,
            (value >> 16) & 0xFF, (value >> 24) & 0xFF,
        ])
        resp = await self.command(cmd, 2)
        self._check_status(
            resp,
            f"write_dap_reg(port=0x{dap_port:04x}, "
            f"addr=0x{addr:04x}, val=0x{value:08x})")

    async def init_ap(self, ap_num: int) -> None:
        """Tell ST-Link to start driving the given AP. Required
        before any non-IDR access on the AP. Idempotent for the
        firmware; cache at the caller to avoid redundant USB
        round-trips."""
        resp = await self.command(
            bytes([protocol.CMD_DEBUG,
                   protocol.DEBUG_APIV2_INIT_AP,
                   ap_num & 0xFF]), 2)
        self._check_status(resp, f"init_ap({ap_num})")

    async def close_ap(self, ap_num: int) -> None:
        """Release a previously-init'd AP."""
        await self.command(
            bytes([protocol.CMD_DEBUG,
                   protocol.DEBUG_APIV2_CLOSE_AP,
                   ap_num & 0xFF]), 2)
        # Some firmwares return non-OK on close even when the
        # operation succeeded — match OpenOCD and don't error.

    # -- Bulk memory access via ST-Link's MEM-AP commands ----------
    #
    # Memory accesses go through dedicated commands rather than
    # poking CSW/TAR/DRW directly — ST-Link manages CSW internally.
    # The 32-bit variants require word alignment on both address
    # and length (which we enforce). Word size: 1..255 words per
    # transfer; longer accesses are chunked at the caller.

    async def read_mem32(self, ap_num: int, addr: int,
                         word_count: int, csw: int = 0) -> bytes:
        """Read ``word_count`` 32-bit words from ``addr`` via MEM-AP
        ``ap_num``. Returns raw little-endian bytes (length =
        word_count * 4)."""
        if word_count == 0:
            return b""
        length = word_count * 4
        cmd = bytes([
            protocol.CMD_DEBUG,
            protocol.DEBUG_READMEM_32BIT,
            addr & 0xFF, (addr >> 8) & 0xFF,
            (addr >> 16) & 0xFF, (addr >> 24) & 0xFF,
            length & 0xFF, (length >> 8) & 0xFF,
            ap_num & 0xFF,
            (csw >> 8) & 0xFF,
            (csw >> 16) & 0xFF,
            (csw >> 24) & 0xFF,
        ])
        data = await self.command(cmd, length)
        await self._get_last_rw_status()
        return data

    async def write_mem32(self, ap_num: int, addr: int, data: bytes,
                          csw: int = 0) -> None:
        """Write ``data`` (little-endian; length must be a multiple
        of 4) to ``addr`` via MEM-AP ``ap_num``."""
        if not data:
            return
        if len(data) % 4 != 0:
            raise ValueError("write_mem32 data length must be a multiple of 4")
        length = len(data)
        cmd = bytes([
            protocol.CMD_DEBUG,
            protocol.DEBUG_WRITEMEM_32BIT,
            addr & 0xFF, (addr >> 8) & 0xFF,
            (addr >> 16) & 0xFF, (addr >> 24) & 0xFF,
            length & 0xFF, (length >> 8) & 0xFF,
            ap_num & 0xFF,
            (csw >> 8) & 0xFF,
            (csw >> 16) & 0xFF,
            (csw >> 24) & 0xFF,
        ])
        async with self._lock:
            self._logger.protocol(
                "ST-Link mem-write32 ap=%d addr=0x%x len=%d",
                ap_num, addr, length)
            await self._ep_out.write(cmd + bytes(_COMMAND_BYTES - len(cmd)))
            await self._ep_out.write(data)
        await self._get_last_rw_status()

    async def _get_last_rw_status(self) -> None:
        """Check status of the previous mem-read/write transaction.
        Memory commands themselves don't return a status — this
        follow-up does. Raises :class:`StLinkError` on non-OK.

        Uses GETLASTRWSTATUS2 (12-byte response) — the form ST-Link
        v3 firmware expects. The legacy 2-byte GETLASTRWSTATUS would
        leave 10 bytes unread on the IN endpoint, wedging the next
        transaction."""
        resp = await self.command(
            bytes([protocol.CMD_DEBUG,
                   protocol.DEBUG_GET_LAST_RW_STATUS2]), 12)
        self._check_status(resp, "get_last_rw_status")

    async def close(self) -> None:
        try:
            self._device.handle.releaseInterface(self._interface)
        except Exception:
            pass
