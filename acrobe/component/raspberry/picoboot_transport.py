"""USB transport for the RP2040 PICOBOOT bootloader.

Concrete `PicobootTransport` implementation over ausb. Framing
follows the RP2040 datasheet 2.8.5 and matches what picotool /
crobe's `BootInterface` send on the wire.

PICOBOOT exposes a single vendor-class interface with one bulk
OUT and one bulk IN endpoint, plus two interface-recipient
vendor control requests:

- ``RESET_INTERFACE`` (bRequest=0x41, OUT, no data) clears any
  wedged command state. Sent on every open as a defensive
  measure — picotool does the same.
- ``GET_COMMAND_STATUS`` (bRequest=0x42, IN, 16 bytes) reads the
  last command's status block. Consulted on error to surface a
  useful failure code.

Command framing (32 bytes per request):

    0  4  dMagic       0x431fd10b
    4  4  dToken       host-chosen, increment per command
    8  1  bCmdId       0x01..0x09 (high bit = IN data direction)
    9  1  bCmdSize     bytes used in cmd_args
    10 2  reserved
    12 4  dTransferLength  data bytes after this command (if any)
    16 16 cmd_args     command-specific, zero-padded

Per-command sequence on the wire:

- ``Write`` (cmd_id=0x05): bulk OUT command; bulk OUT data;
  bulk IN reads a zero-length ack packet.
- ``Read`` (cmd_id=0x84): bulk OUT command; bulk IN data (loop
  until size received or short packet); bulk OUT zero-length ack.
- ``Exec`` (cmd_id=0x08): bulk OUT command; bulk IN zero-length
  ack — the bootrom holds this packet until the called function
  returns, so a successful IN read implicitly means "function
  returned".

On bulk-level errors (e.g. STALL), the transport reads
``GET_COMMAND_STATUS``, resets the interface, and raises
``PicobootError`` carrying the status code from the bootrom.
"""

from __future__ import annotations

import asyncio
import logging
import struct

import usb1
from ausb.handle import (
    BulkOutEndpoint, BulkInEndpoint,
    RequestTypeType, RequestTypeRecipient,
)
from ausb.exception import TransferError, TransferTimeout, TransferStalled


PICOBOOT_MAGIC = 0x431FD10B

# Command IDs. High bit set = device-to-host data direction.
CMD_EXCLUSIVE_ACCESS = 0x01
CMD_REBOOT           = 0x02
CMD_FLASH_ERASE      = 0x03
CMD_READ             = 0x84
CMD_WRITE            = 0x05
CMD_EXIT_XIP         = 0x06
CMD_ENTER_CMD_XIP    = 0x07
CMD_EXEC             = 0x08
CMD_VECTORIZE_FLASH  = 0x09

# Interface-recipient vendor control requests.
CTRL_RESET_INTERFACE    = 0x41
CTRL_GET_COMMAND_STATUS = 0x42

# Bootrom status codes (see RP2040 datasheet 2.8.5.4).
STATUS_OK                       = 0
STATUS_UNKNOWN_CMD              = 1
STATUS_INVALID_CMD_LENGTH       = 2
STATUS_INVALID_TRANSFER_LENGTH  = 3
STATUS_INVALID_ADDRESS          = 4
STATUS_BAD_ALIGNMENT            = 5
STATUS_INTERLEAVED_WRITE        = 6
STATUS_REBOOTING                = 7
STATUS_UNKNOWN_ERROR            = 8

_STATUS_NAMES = {
    STATUS_OK:                      "OK",
    STATUS_UNKNOWN_CMD:             "UNKNOWN_CMD",
    STATUS_INVALID_CMD_LENGTH:      "INVALID_CMD_LENGTH",
    STATUS_INVALID_TRANSFER_LENGTH: "INVALID_TRANSFER_LENGTH",
    STATUS_INVALID_ADDRESS:         "INVALID_ADDRESS",
    STATUS_BAD_ALIGNMENT:           "BAD_ALIGNMENT",
    STATUS_INTERLEAVED_WRITE:       "INTERLEAVED_WRITE",
    STATUS_REBOOTING:               "REBOOTING",
    STATUS_UNKNOWN_ERROR:           "UNKNOWN_ERROR",
}


USB_VID_RPI = 0x2E8A
USB_PID_RP2040_BOOTSEL = 0x0003


class PicobootError(Exception):
    """A PICOBOOT command failed. ``status`` is the bootrom code."""

    def __init__(self, status: int, command: int, message: str = ""):
        name = _STATUS_NAMES.get(status, f"0x{status:08x}")
        prefix = (f"PICOBOOT cmd 0x{command:02x} failed: "
                  f"status={name}")
        super().__init__(f"{prefix} ({message})" if message else prefix)
        self.status = status
        self.command = command


def build_command(token: int, cmd_id: int, transfer_length: int,
                  args: bytes) -> bytes:
    """Serialise one PICOBOOT command header (32 bytes).

    ``args`` is up to 16 bytes; bytes beyond its length are
    zero-padded. ``cmd_id``'s high bit is preserved (callers
    decide the direction).
    """
    if len(args) > 16:
        raise ValueError(f"args too long: {len(args)} > 16")
    header = struct.pack(
        "<IIBBHI",
        PICOBOOT_MAGIC,
        token & 0xFFFFFFFF,
        cmd_id & 0xFF,
        len(args),
        0,
        transfer_length & 0xFFFFFFFF)
    return header + args + bytes(16 - len(args))


def parse_status(blob: bytes) -> tuple[int, int, int, int]:
    """Decode a 16-byte status block. Returns
    ``(token, status_code, cmd_id, in_progress)``.

    The trailing 6 bytes are reserved padding."""
    if len(blob) < 16:
        raise ValueError(f"status blob short: {len(blob)} < 16")
    token, status_code, cmd_id, in_progress = struct.unpack(
        "<IIBB", blob[:10])
    return token, status_code, cmd_id, in_progress


class PicobootUsbTransport:
    """`PicobootTransport` implementation over ausb.

    Construction: use the :meth:`from_device` classmethod with an
    opened ausb ``Device``. The transport claims the PICOBOOT
    interface and discovers its bulk endpoints. ``close()``
    releases the interface.

    A single ``asyncio.Lock`` serialises commands; concurrent
    coroutines won't interleave bulk transactions.
    """

    DEFAULT_TIMEOUT_S = 1.0
    EXEC_TIMEOUT_S = 30.0

    def __init__(self, device, interface_index: int,
                 ep_out: BulkOutEndpoint, ep_in: BulkInEndpoint,
                 mps: int, logger: logging.Logger):
        self._device = device
        self._interface = interface_index
        self._ep_out = ep_out
        self._ep_in = ep_in
        self._mps = mps
        self._lock = asyncio.Lock()
        self._logger = logger
        self._token = 0

    @classmethod
    async def from_device(cls, device, *,
                          logger: logging.Logger | None = None
                          ) -> "PicobootUsbTransport":
        if logger is None:
            logger = logging.getLogger("picoboot.transport")

        iface, ep_out_addr, ep_in_addr, mps = cls._find_interface(device)

        try:
            device.handle.detachKernelDriver(iface)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported,
                usb1.USBErrorAccess):
            pass
        device.handle.claimInterface(iface)

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)

        logger.debug("PICOBOOT iface=%d EP_OUT=0x%02x EP_IN=0x%02x mps=%d",
                     iface, ep_out_addr, ep_in_addr, mps)

        transport = cls(device, iface, ep_out, ep_in, mps, logger)
        # Defensive reset — clears state from a previous session that
        # may have died mid-command. Idempotent.
        await transport.reset_interface()
        return transport

    @staticmethod
    def _find_interface(device):
        """Locate the PICOBOOT interface — first vendor-class interface
        (class 0xFF, subclass 0, protocol 0) with a bulk OUT and bulk
        IN endpoint. Returns
        ``(interface_index, ep_out_addr, ep_in_addr, mps)``."""
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
                return (i, ep_out_addr, ep_in_addr,
                        max(ep_out_mps, ep_in_mps))
        raise RuntimeError(
            "PICOBOOT: no vendor-class interface with bulk OUT+IN found")

    async def reset_interface(self):
        """``RESET_INTERFACE`` (control OUT 0x41). Aborts any pending
        command in the bootrom. Idempotent."""
        await self._device.control(
            RequestTypeType.Vendor, RequestTypeRecipient.Interface,
            CTRL_RESET_INTERFACE, 0, self._interface, b"")
        self._logger.debug("PICOBOOT reset_interface")

    async def read_command_status(self) -> tuple[int, int, int, int]:
        """``GET_COMMAND_STATUS`` (control IN 0x42). Returns
        ``(token, status_code, cmd_id, in_progress)``."""
        blob = await self._device.control(
            RequestTypeType.Vendor, RequestTypeRecipient.Interface,
            CTRL_GET_COMMAND_STATUS, 0, self._interface, 16)
        return parse_status(blob)

    def _next_token(self):
        self._token = (self._token + 1) & 0xFFFFFFFF
        return self._token

    async def _bulk_in_until(self, size: int) -> bytes:
        """Read up to ``size`` bytes from the bulk-IN endpoint. Stop
        early on a short packet (the bootrom's end-of-frame marker)."""
        if size == 0:
            # ZLP ack — request MPS but accept whatever lands (0..MPS).
            chunk = await self._ep_in.read(self._mps)
            return bytes(chunk)
        buf = bytearray()
        while len(buf) < size:
            chunk = await self._ep_in.read(self._mps)
            buf.extend(chunk)
            if len(chunk) < self._mps:
                break
        return bytes(buf[:size])

    async def _execute(self, cmd_id: int, args: bytes, *,
                       in_size: int = 0,
                       out_data: bytes | None = None,
                       timeout: float = DEFAULT_TIMEOUT_S) -> bytes:
        """Run one PICOBOOT command cycle.

        Exactly one of ``in_size > 0`` and ``out_data is not None``
        may be non-trivial — they are mutually exclusive directions
        on the bulk endpoint.

        On bulk-level error, reads the bootrom status, resets the
        interface, and raises :class:`PicobootError`.
        """
        if out_data is not None and in_size > 0:
            raise ValueError(
                "command cannot have both IN and OUT data")
        transfer_length = (
            in_size if in_size > 0
            else (len(out_data) if out_data else 0))
        token = self._next_token()
        header = build_command(token, cmd_id, transfer_length, args)

        async with self._lock:
            self._logger.protocol(
                "PICOBOOT cmd 0x%02x token=%d transfer=%d",
                cmd_id, token, transfer_length)
            try:
                return await asyncio.wait_for(
                    self.__execute_locked(
                        cmd_id, header, in_size, out_data),
                    timeout=timeout)
            except (TransferError, TransferStalled,
                    TransferTimeout, asyncio.TimeoutError) as e:
                await self.__surface_error(cmd_id, e)
                raise  # __surface_error always raises, but keep typed

    async def __execute_locked(self, cmd_id, header, in_size, out_data):
        await self._ep_out.write(header)
        if cmd_id & 0x80:
            # IN direction: read data, then send ZLP ack.
            data = await self._bulk_in_until(in_size)
            await self._ep_out.write(b"")
            return data
        # OUT direction: send data (if any), then read ZLP ack. The
        # bootrom holds this IN packet until the command completes
        # (including the EXEC'd function returning).
        if out_data:
            await self._ep_out.write(out_data)
        await self._bulk_in_until(0)
        return b""

    async def __surface_error(self, cmd_id, original):
        """Best-effort: read the bootrom status to surface a useful
        code, reset the interface, then raise PicobootError."""
        try:
            _, status, last_cmd, _ = await self.read_command_status()
        except Exception:
            status, last_cmd = STATUS_UNKNOWN_ERROR, cmd_id
        try:
            await self.reset_interface()
        except Exception:
            pass
        raise PicobootError(status, last_cmd, str(original))

    # -- PicobootTransport surface (Protocol-conforming) --------------

    async def read(self, addr: int, size: int) -> bytes:
        if size == 0:
            return b""
        args = struct.pack("<II", addr, size)
        return await self._execute(CMD_READ, args, in_size=size)

    async def write(self, addr: int, data: bytes) -> None:
        if not data:
            return
        args = struct.pack("<II", addr, len(data))
        await self._execute(CMD_WRITE, args, out_data=bytes(data))

    async def exec(self, pc: int) -> None:
        # Address must have the Thumb bit set on Cortex-M0+. The
        # puppet does this already; OR-in here defensively too.
        args = struct.pack("<I", pc | 1)
        await self._execute(CMD_EXEC, args, timeout=self.EXEC_TIMEOUT_S)

    # -- Other PICOBOOT operations ------------------------------------

    async def exclusive_access(self, exclusive: int) -> None:
        """Take/release exclusive access. 0=shared, 1=exclusive,
        2=exclusive-eject (kicks USB MSC out of the way)."""
        args = struct.pack("<B", exclusive & 0xFF)
        await self._execute(CMD_EXCLUSIVE_ACCESS, args)

    async def exit_xip(self) -> None:
        """Take the QSPI flash out of XIP mode so direct register
        access works. Required before driving SSI by hand."""
        await self._execute(CMD_EXIT_XIP, b"")

    async def enter_cmd_xip(self) -> None:
        """Re-enter XIP mode using the bootrom's default sequence."""
        await self._execute(CMD_ENTER_CMD_XIP, b"")

    async def flash_erase(self, addr: int, size: int) -> None:
        if addr & 0xFFF or size & 0xFFF:
            raise ValueError(
                f"flash_erase must be 4 KiB aligned: "
                f"addr=0x{addr:08x} size=0x{size:08x}")
        args = struct.pack("<II", addr, size)
        await self._execute(CMD_FLASH_ERASE, args,
                            timeout=self.EXEC_TIMEOUT_S)

    async def vectorize_flash(self, addr: int) -> None:
        """Reboot using a vector table read from flash at ``addr``."""
        args = struct.pack("<I", addr)
        await self._execute(CMD_VECTORIZE_FLASH, args)

    async def reboot(self, pc: int = 0, sp: int = 0,
                     delay_ms: int = 100) -> None:
        """``pc=0`` reboots into bootrom again (BOOTSEL stays). Any
        other ``pc`` jumps to that address with ``sp`` after the
        delay. Errors are swallowed because the device disappears
        from USB before the ack arrives."""
        args = struct.pack("<III", pc, sp, delay_ms)
        try:
            await self._execute(CMD_REBOOT, args, timeout=0.5)
        except (PicobootError, TimeoutError, TransferError):
            pass

    async def close(self) -> None:
        try:
            self._device.handle.releaseInterface(self._interface)
        except Exception:
            pass
