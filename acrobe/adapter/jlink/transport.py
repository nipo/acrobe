"""J-Link USB transport.

Single-byte command opcode + variable-length arguments out, variable
response in. Endpoints discovered by scanning for the first
vendor-class interface on the device with bulk OUT + bulk IN — same
pattern libjaylink uses.

Synchronous serialisation via an asyncio.Lock so concurrent coroutines
don't interleave commands.
"""

from __future__ import annotations

import asyncio
import logging
import usb1
from ausb.handle import BulkOutEndpoint, BulkInEndpoint

from . import protocol


class JLinkTransport:
    """Async USB transport for SEGGER J-Link debug adapters."""

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
    async def from_device(cls, device, *,
                          logger: logging.Logger | None = None
                          ) -> "JLinkTransport":
        """Find the first vendor-class interface with bulk OUT + IN
        endpoints, claim it, and return a transport bound to that
        endpoint pair."""
        if logger is None:
            logger = logging.getLogger("jlink.transport")

        interface_index, ep_out_addr, ep_in_addr, mps = \
            cls._find_interface(device)

        try:
            device.handle.detachKernelDriver(interface_index)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported,
                usb1.USBErrorAccess):
            pass
        device.handle.claimInterface(interface_index)

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)

        logger.debug("J-Link USB iface=%d EP_OUT=0x%02x EP_IN=0x%02x mps=%d",
                     interface_index, ep_out_addr, ep_in_addr, mps)

        # Drain stale bytes left by a previous session.
        from ausb.exception import TransferTimeout
        try:
            while True:
                ep_in.read_sync(mps, timeout=20)
        except TransferTimeout:
            pass

        return cls(device, interface_index, ep_out, ep_in, logger)

    @staticmethod
    def _find_interface(device):
        """Locate the J-Link's debug interface — first vendor-class
        interface with at least one bulk-OUT and one bulk-IN
        endpoint. Returns (interface_index, ep_out_addr, ep_in_addr,
        max_packet_size)."""
        config = device.descriptor[device.configuration]
        BULK = 2
        VENDOR_CLASS = 0xFF

        for i, interface in enumerate(config):
            setting = interface[0]
            if setting.classes[0] != VENDOR_CLASS:
                continue
            ep_out_addr = ep_in_addr = None
            ep_out_mps = ep_in_mps = 0
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
            if ep_out_addr is not None and ep_in_addr is not None:
                return i, ep_out_addr, ep_in_addr, max(ep_out_mps, ep_in_mps)

        raise RuntimeError(
            "J-Link: no vendor-class interface with bulk OUT+IN found")

    # -- Low-level write / read ------------------------------------

    async def _write(self, data: bytes) -> None:
        await self._ep_out.write(data)

    async def _read(self, length: int) -> bytes:
        if length == 0:
            return b""
        return await self._ep_in.read(length)

    # -- High-level commands ---------------------------------------

    async def get_firmware_version(self) -> str:
        """Read the firmware version string (e.g. "J-Link OB-K22-SiFive
        compiled Jan 14 2020 ...")."""
        async with self._lock:
            await self._write(bytes([protocol.CMD_GET_VERSION]))
            length_bytes = await self._read(2)
            length = length_bytes[0] | (length_bytes[1] << 8)
            if length == 0:
                return ""
            data = await self._read(length)
        return data.rstrip(b"\x00").decode("utf-8", errors="replace")

    async def get_hardware_version(self) -> tuple[int, int, int, int]:
        """Returns (type, major, minor, revision). Encoded as a 32-bit
        LE integer: type * 1_000_000 + major * 10_000 + minor * 100 +
        revision."""
        async with self._lock:
            await self._write(bytes([protocol.CMD_GET_HW_VERSION]))
            data = await self._read(4)
        v = (data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24))
        return (v // 1_000_000,
                (v // 10_000) % 100,
                (v // 100) % 100,
                v % 100)

    async def get_caps(self) -> bytes:
        """Read the 32-bit capability bitmap. Returns 4 bytes
        little-endian."""
        async with self._lock:
            await self._write(bytes([protocol.CMD_GET_CAPS]))
            return await self._read(4)

    async def get_ext_caps(self) -> bytes:
        """Read the 256-bit extended capability bitmap. Returns 32
        bytes."""
        async with self._lock:
            await self._write(bytes([protocol.CMD_GET_EXT_CAPS]))
            return await self._read(32)

    async def select_interface(self, tif: int) -> int:
        """Switch the target interface (JTAG / SWD / ...). Returns
        the previously-selected interface."""
        async with self._lock:
            await self._write(bytes([protocol.CMD_SELECT_TIF, tif & 0xFF]))
            data = await self._read(4)
        return data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)

    async def set_speed_khz(self, speed_khz: int) -> None:
        """Set the JTAG/SWD clock speed in kHz."""
        async with self._lock:
            await self._write(bytes([
                protocol.CMD_SET_SPEED,
                speed_khz & 0xFF,
                (speed_khz >> 8) & 0xFF,
            ]))

    async def assert_reset(self) -> None:
        """Drive nRST low."""
        async with self._lock:
            await self._write(bytes([protocol.CMD_SET_RESET]))

    async def deassert_reset(self) -> None:
        """Release nRST."""
        async with self._lock:
            await self._write(bytes([protocol.CMD_CLEAR_RESET]))

    async def jtag_io(self, tms: bytes, tdi: bytes,
                      bit_count: int) -> bytes:
        """Issue a JTAG_IO_V3 transaction.

        ``tms`` and ``tdi`` are byte arrays, LSB-first per JTAG
        convention; both must be ``ceil(bit_count / 8)`` bytes long.
        Returns ``num_bytes`` bytes of TDO.

        Frame: cmd(1) + 0(1) + length_bits_LE(2) + tms_bytes + tdi_bytes
        Response: tdo_bytes + status_byte (status != 0 means error)."""
        num_bytes = (bit_count + 7) // 8
        if len(tms) != num_bytes or len(tdi) != num_bytes:
            raise ValueError(
                f"jtag_io: tms/tdi length mismatch "
                f"(bit_count={bit_count}, expected {num_bytes} bytes)")
        cmd = bytes([protocol.CMD_JTAG_IO_V3, 0,
                     bit_count & 0xFF, (bit_count >> 8) & 0xFF])
        async with self._lock:
            await self._write(cmd + tms + tdi)
            tdo = await self._read(num_bytes)
            status = await self._read(1)
        if status[0] != 0:
            raise protocol.JLinkError(
                f"JTAG_IO failed with status 0x{status[0]:02x}")
        return tdo

    async def close(self) -> None:
        try:
            self._device.handle.releaseInterface(self._interface)
        except Exception:
            pass
