"""Async USB bulk transport for the XDS110 firmware.

A single :meth:`XDS110Transport.command` call:

  1. Wraps the supplied opcode + parameter payload in a sync byte +
     LE u16 size header (see :class:`.protocol.Frame`) and writes it
     to the bulk OUT endpoint as one transfer.
  2. Reads from bulk IN until the announced payload size has arrived,
     re-syncing on a bad header (the firmware can leave stale bytes
     queued from a previous session that died mid-response).
  3. Validates the leading 4-byte u32 status, raising
     :class:`.protocol.XDS110Error` on a non-zero code.
  4. Returns the response payload with the status word stripped."""

from __future__ import annotations

import asyncio
import logging

import usb1
from ausb.handle import BulkInEndpoint, BulkOutEndpoint
from ausb.exception import TransferTimeout

from . import protocol


class XDS110Transport:
    """One claimed debug interface, one bulk OUT, one bulk IN."""

    def __init__(self, device, interface_index: int,
                 ep_out: BulkOutEndpoint, ep_in: BulkInEndpoint,
                 mps: int, logger: logging.Logger):
        self.__device = device
        self.__interface = interface_index
        self.__ep_out = ep_out
        self.__ep_in = ep_in
        self.__mps = mps
        self.__lock = asyncio.Lock()
        self.__logger = logger

    @classmethod
    async def from_device(cls, device, *, interface_index: int,
                          ep_out_addr: int, ep_in_addr: int,
                          logger: logging.Logger | None = None
                          ) -> "XDS110Transport":
        """Claim the debug interface and bind the two endpoints.

        VID/PID-specific addresses come from the adapter's static
        table — XDS110 SKUs split between (interface=2, EPs 0x02/0x83)
        and (interface=0, EPs 0x01/0x81), and we don't bother
        scanning the descriptors because the firmware ID also varies."""
        if logger is None:
            logger = logging.getLogger("xds110.transport")

        try:
            device.handle.detachKernelDriver(interface_index)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported,
                usb1.USBErrorAccess):
            pass
        device.handle.claimInterface(interface_index)

        mps = cls.__endpoint_mps(device, interface_index,
                                ep_out_addr, ep_in_addr)
        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)

        # Drain stale IN bytes left over from a previous session.
        # Without this, a half-read response from a prior process
        # de-syncs the very first command.
        try:
            while True:
                ep_in.read_sync(mps, timeout=20)
        except TransferTimeout:
            pass

        logger.debug("XDS110 USB iface=%d EP_OUT=0x%02x EP_IN=0x%02x mps=%d",
                     interface_index, ep_out_addr, ep_in_addr, mps)
        return cls(device, interface_index, ep_out, ep_in, mps, logger)

    @staticmethod
    def __endpoint_mps(device, interface_index: int,
                      ep_out_addr: int, ep_in_addr: int) -> int:
        """Return the larger of the two bulk endpoints' max packet
        sizes — used as the natural granule for IN reads."""
        config = device.descriptor[device.configuration]
        setting = config[interface_index][0]
        out_mps = in_mps = 0
        for ep in setting:
            if ep.address == ep_out_addr:
                out_mps = ep.max_packet_size
            elif ep.address == ep_in_addr:
                in_mps = ep.max_packet_size
        if not out_mps or not in_mps:
            raise RuntimeError(
                f"XDS110: endpoints 0x{ep_out_addr:02x} / 0x{ep_in_addr:02x} "
                f"not found on interface {interface_index}")
        return max(out_mps, in_mps)

    async def command(self, payload: bytes,
                      response_payload_size: int,
                      *, timeout_ms: int = protocol.DEFAULT_TIMEOUT_MS
                      ) -> bytes:
        """Issue one request and read back its response.

        ``payload`` is the inner payload starting with the opcode byte.
        ``response_payload_size`` is the EXACT expected size of the
        response payload — opcode-specific, includes the leading
        4-byte status word. Returns the response payload with that
        status word stripped (so callers see only the opcode-specific
        result bytes); raises :class:`.protocol.XDS110Error` on a
        non-zero status."""
        frame = protocol.Frame.encode(payload)
        opcode = protocol.Opcode(payload[0])

        async with self.__lock:
            self.__logger.protocol(
                "XDS110 -> op=%s, len=%d", opcode.name, len(payload))
            await self.__ep_out.write(frame)
            response = await self.__read_response(response_payload_size,
                                                 timeout_ms)

        status = protocol.Bytes.unpack_i32(response, 0)
        result = bytes(response[protocol.ERROR_CODE_LEN:])
        self.__logger.protocol(
            "XDS110 <- op=%s, status=%d result=%dB",
            opcode.name, status, len(result))
        if status != protocol.SC_ERR_NONE:
            raise protocol.XDS110Error(status, f"op=0x{opcode:02x}")
        return result

    async def __read_response(self, expected_payload_size: int,
                             timeout_ms: int) -> bytes:
        """Read one full response, tolerating stale leading bytes.

        The firmware can leave junk in the IN endpoint when a previous
        command timed out mid-response. Read MPS-sized chunks; on a
        valid header we cut the payload out of the buffer and keep
        reading until the announced size is met, otherwise we drop
        the chunk and try again."""
        # Loop until we land on a valid '*'+size header.
        while True:
            chunk = await self.__read_chunk(timeout_ms)
            if (len(chunk) >= protocol.HEADER_LEN
                    and chunk[0] == protocol.SYNC_BYTE):
                announced = protocol.Frame.parse_header(chunk)
                if announced != expected_payload_size:
                    # Caller asked for a different size than the
                    # firmware delivered — almost always a coding bug
                    # on our end, surface it as a hard error.
                    raise protocol.XDS110Error(
                        protocol.SC_ERR_XDS110_FAIL,
                        f"expected payload {expected_payload_size}B, "
                        f"firmware returned {announced}B")
                buf = bytearray(chunk[protocol.HEADER_LEN:])
                # Already received this much of the payload.
                while len(buf) < announced:
                    more = await self.__read_chunk(
                        timeout_ms, expected_max=announced - len(buf))
                    buf.extend(more)
                if len(buf) > announced:
                    raise protocol.XDS110Error(
                        protocol.SC_ERR_XDS110_FAIL,
                        f"response overrun: got {len(buf)}B for "
                        f"{announced}B payload")
                return bytes(buf)
            self.__logger.warning(
                "XDS110: discarding %d-byte unsynced chunk", len(chunk))

    async def __read_chunk(self, timeout_ms: int,
                          *, expected_max: int | None = None) -> bytes:
        """Read at most one MPS chunk from bulk IN."""
        size = self.__mps if expected_max is None else min(self.__mps,
                                                          expected_max)
        # ausb's BulkInEndpoint.read returns once one transfer has
        # completed — a USB IN transfer terminates at MPS or short
        # packet, which matches one firmware response chunk.
        return await self.__ep_in.read(size, timeout=timeout_ms)

    async def close(self) -> None:
        try:
            self.__device.handle.releaseInterface(self.__interface)
        except Exception:
            pass
