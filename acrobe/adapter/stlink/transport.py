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

    async def close(self) -> None:
        try:
            self._device.handle.releaseInterface(self._interface)
        except Exception:
            pass
