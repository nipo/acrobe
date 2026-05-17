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
                 mps: int,
                 logger: logging.Logger):
        self.__device = device
        self.__interface = interface_index
        self.__ep_out = ep_out
        self.__ep_in = ep_in
        self.__mps = mps
        self.__lock = asyncio.Lock()
        self.__logger = logger
        # JTAG_IO_V3 (with status byte) is only available on hardware
        # major version ≥ 5; older OB hardware uses V2. Set by the
        # adapter after reading hardware_version.
        self.jtag_io_v3 = False

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
            cls.__find_interface(device)

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

        return cls(device, interface_index, ep_out, ep_in, mps, logger)

    @staticmethod
    def __find_interface(device):
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

    async def __write(self, data: bytes) -> None:
        await self.__ep_out.write(data)

    async def __read(self, length: int) -> bytes:
        """Read at least ``length`` bytes from the bulk-IN endpoint.

        Issues MPS-sized reads in a loop and accumulates the result.
        A short packet (< MPS bytes) is the device's way of saying
        "frame is over" — stop reading even if we have fewer than
        ``length`` total bytes; the caller decides whether that's
        an error. Truncates to ``length`` on return."""
        if length == 0:
            return b""
        out = bytearray()
        while len(out) < length:
            chunk = await self.__ep_in.read(self.__mps)
            out.extend(chunk)
            if len(chunk) < self.__mps:
                break
        return bytes(out[:length])

    # -- High-level commands ---------------------------------------

    async def get_firmware_version(self) -> str:
        """Read the firmware version string (e.g. "J-Link OB-K22-SiFive
        compiled Jan 14 2020 ...")."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_GET_VERSION]))
            length_bytes = await self.__read(2)
            length = length_bytes[0] | (length_bytes[1] << 8)
            if length == 0:
                return ""
            data = await self.__read(length)
        return data.rstrip(b"\x00").decode("utf-8", errors="replace")

    async def get_hardware_version(self) -> tuple[int, int, int, int]:
        """Returns (type, major, minor, revision). Encoded as a 32-bit
        LE integer: type * 1_000_000 + major * 10_000 + minor * 100 +
        revision."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_GET_HW_VERSION]))
            data = await self.__read(4)
        v = (data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24))
        return (v // 1_000_000,
                (v // 10_000) % 100,
                (v // 100) % 100,
                v % 100)

    async def get_caps(self) -> bytes:
        """Read the 32-bit capability bitmap. Returns 4 bytes
        little-endian."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_GET_CAPS]))
            return await self.__read(4)

    async def get_ext_caps(self) -> bytes:
        """Read the 256-bit extended capability bitmap. Returns 32
        bytes."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_GET_EXT_CAPS]))
            return await self.__read(32)

    async def select_interface(self, tif: int) -> int:
        """Switch the target interface (JTAG / SWD / ...). Returns
        the previously-selected interface."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_SELECT_TIF, tif & 0xFF]))
            data = await self.__read(4)
        return data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)

    async def get_available_interfaces(self) -> int:
        """Returns the bitmap of supported interfaces (bit 0=JTAG,
        bit 1=SWD, ...)."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_SELECT_TIF,
                                     protocol.TIF_GET_AVAILABLE]))
            data = await self.__read(4)
        return data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)

    async def set_speed_khz(self, speed_khz: int) -> None:
        """Set the JTAG/SWD clock speed in kHz."""
        async with self.__lock:
            await self.__write(bytes([
                protocol.CMD_SET_SPEED,
                speed_khz & 0xFF,
                (speed_khz >> 8) & 0xFF,
            ]))

    async def get_speeds(self) -> tuple[int, int]:
        """Read the device's base frequency (Hz) and minimum
        divider for the currently-selected target interface.

        Some early OB firmwares apparently need this command to
        commit interface-related state — e.g. on EFM32 OB after a
        cold power-up, SWD bit-bang silently misbehaves until
        GET_SPEEDS has been issued at least once. (JLinkExe always
        runs it during init; we now do too.) Caller's responsibility
        to gate on :data:`CAP_SPEED_INFO`."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_GET_SPEEDS]))
            data = await self.__read(6)
            self.__logger.debug("GET_SPEEDS raw: %s (len=%d)",
                               data.hex(), len(data))
        base_freq = (data[0] | (data[1] << 8)
                     | (data[2] << 16) | (data[3] << 24))
        min_div = data[4] | (data[5] << 8)
        return base_freq, min_div

    async def register(self, enable: bool, handle: int = 0) -> int:
        """Claim (``enable=True``) or release (``enable=False``) an
        exclusive connection handle on the J-Link.

        The firmware tracks active host connections so concurrent
        users (e.g. acrobe + JLinkExe + GDB) see each other and don't
        clobber per-connection state. JLinkExe and crobe both register
        on connect when CAP_REGISTER is set; we follow suit.

        Frame: cmd(1) + sub(1, 0x64=register / 0x65=unregister) +
        pid(u32 LE) + addr(u32 LE) + iid(1) + cid(1) + handle(u16 LE).
        Response: handle(u16 LE) + count(u16 LE) + entry_size(u16 LE) +
        info_size(u16 LE) + count*entry_size connection-info entries +
        info_size trailer. Returns the handle the J-Link assigned us
        (zero for unregister)."""
        import os
        import struct
        sub = 0x64 if enable else 0x65
        payload = struct.pack(
            "<BBIIBBH",
            protocol.CMD_REGISTER, sub, os.getpid() & 0xFFFFFFFF,
            0, 0, 0, handle & 0xFFFF)
        async with self.__lock:
            await self.__write(payload)
            # Response is variable length — read one MPS-or-larger
            # buffer; the loop terminates on the device's short-packet
            # marker.
            resp = await self.__read(2048)
        if len(resp) < 8:
            raise protocol.JLinkError(
                f"REGISTER short response: got {len(resp)} bytes")
        new_handle, count, entry_size, info_size = struct.unpack_from(
            "<HHHH", resp, 0)
        expected = 8 + count * entry_size + info_size
        if len(resp) < expected:
            raise protocol.JLinkError(
                f"REGISTER response truncated: got {len(resp)} bytes, "
                f"expected {expected} (handle=0x{new_handle:04x}, "
                f"count={count}, entry_size={entry_size}, "
                f"info_size={info_size})")
        return new_handle

    async def get_hw_status(self) -> dict:
        """Read the 8-byte hardware status block: target voltage in
        mV plus pin states (TCK, TDI, TDO, TMS, TRES, TRST)."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_GET_HW_STATUS]))
            data = await self.__read(8)
        return {
            "target_voltage_mv": data[0] | (data[1] << 8),
            "tck": data[2], "tdi": data[3], "tdo": data[4],
            "tms": data[5], "tres": data[6], "trst": data[7],
        }

    async def assert_reset(self) -> None:
        """Drive nRST low (assert reset)."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_HW_RESET0]))

    async def deassert_reset(self) -> None:
        """Drive nRST high (release reset)."""
        async with self.__lock:
            await self.__write(bytes([protocol.CMD_HW_RESET1]))

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
        opcode = (protocol.CMD_JTAG_IO_V3 if self.jtag_io_v3
                  else protocol.CMD_JTAG_IO_V2)
        # V3 appends a status byte to the response; V2 does not.
        resp_size = num_bytes + 1 if self.jtag_io_v3 else num_bytes
        cmd = bytes([opcode, 0,
                     bit_count & 0xFF, (bit_count >> 8) & 0xFF])
        async with self.__lock:
            self.__logger.protocol(
                "JTAG_IO bits=%d tms=%s tdi=%s",
                bit_count, tms[:8].hex(), tdi[:8].hex())
            await self.__write(cmd + tms + tdi)
            resp = await self.__read(resp_size)
            self.__logger.protocol(
                "JTAG_IO tdo=%s (%d bytes)",
                resp[:8].hex(), len(resp))
        if len(resp) < resp_size:
            raise protocol.JLinkError(
                f"JTAG_IO short response: got {len(resp)} bytes, "
                f"expected {resp_size}")
        if self.jtag_io_v3:
            status = resp[num_bytes]
            if status != 0:
                raise protocol.JLinkError(
                    f"JTAG_IO failed with status 0x{status:02x}")
        return resp[:num_bytes]

    async def swd_io(self, direction: bytes, out: bytes,
                     bit_count: int) -> bytes:
        """Issue an SWD bit-bang transaction.

        ``direction`` and ``out`` are LSB-first bit-streams of
        ``ceil(bit_count / 8)`` bytes each. ``direction`` selects who
        drives SWDIO each cycle (1 = host, 0 = target). ``out`` is
        the bits the host drives when direction=1 (ignored in
        target-driven cycles).

        Wire format is identical to JTAG_IO_V2 / JTAG_IO_V3 — when
        the J-Link's TIF is set to SWD, the firmware reinterprets
        the JTAG bit-bang command's TMS slot as the per-cycle output
        enable. crobe and JLinkExe both go through this same opcode
        for SWD; reusing :meth:`jtag_io` keeps the V2/V3 selection
        consistent with hardware-version detection (older J-Link OB
        firmwares (hw major < 5) only respond correctly to V2)."""
        return await self.jtag_io(direction, out, bit_count)

    async def close(self) -> None:
        try:
            self.__device.handle.releaseInterface(self.__interface)
        except Exception:
            pass
