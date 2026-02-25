from __future__ import annotations

import asyncio
import usb1


# FTDI USB vendor requests
SIO_RESET = 0x00
SIO_SET_LATENCY_TIMER = 0x09
SIO_SET_BITMODE = 0x0B

# SIO_RESET sub-commands
SIO_RESET_SIO = 0
SIO_RESET_PURGE_RX = 1
SIO_RESET_PURGE_TX = 2

# Bitmode values
BITMODE_RESET = 0x00
BITMODE_MPSSE = 0x02

# bmRequestType for vendor OUT request to device
VENDOR_OUT = 0x40


class FtdiTransport:
    """FTDI USB transport implementing the MPSSE Transport protocol.

    Uses raw usb1 (libusb1) directly rather than ausb, because ausb's
    async event loop integration has reliability issues with FTDI's
    control transfers and bulk I/O. Bulk transfers are offloaded to
    a thread executor for async compatibility.

    Handles FTDI-specific USB initialization (reset, purge, latency,
    MPSSE bitmode) and modem status byte stripping on reads.
    """

    def __init__(self, ctx, handle, interface_index, ep_out, ep_in, max_packet_size):
        self._ctx = ctx
        self._handle = handle
        self._interface_index = interface_index
        self._ep_out = ep_out
        self._ep_in = ep_in
        self._max_packet_size = max_packet_size

    @classmethod
    async def open(cls, *, vid, pid, interface_index=0):
        """Open an FTDI device and initialize MPSSE mode.

        Args:
            vid: USB Vendor ID
            pid: USB Product ID
            interface_index: USB interface index (0=A, 1=B)
        """
        ctx = usb1.USBContext()
        handle = ctx.openByVendorIDAndProductID(vid, pid)
        if handle is None:
            raise ValueError(f"No device found with VID={vid:#06x} PID={pid:#06x}")

        try:
            handle.detachKernelDriver(interface_index)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported):
            pass

        handle.claimInterface(interface_index)

        # FTDI uses 1-based interface index in control request index field
        idx = interface_index + 1

        handle.controlWrite(VENDOR_OUT, SIO_RESET, SIO_RESET_SIO, idx, b'')
        handle.controlWrite(VENDOR_OUT, SIO_SET_LATENCY_TIMER, 1, idx, b'')
        handle.controlWrite(VENDOR_OUT, SIO_SET_BITMODE, BITMODE_RESET << 8, idx, b'')
        handle.controlWrite(VENDOR_OUT, SIO_RESET, SIO_RESET_PURGE_RX, idx, b'')
        handle.controlWrite(VENDOR_OUT, SIO_RESET, SIO_RESET_PURGE_TX, idx, b'')
        handle.controlWrite(VENDOR_OUT, SIO_SET_BITMODE, BITMODE_MPSSE << 8, idx, b'')

        # FTDI endpoint addresses: chan A = OUT 0x02/IN 0x81, chan B = OUT 0x04/IN 0x83
        ep_out = 0x02 + interface_index * 2
        ep_in = 0x81 + interface_index * 2

        # Drain stale data from FTDI buffers
        try:
            while True:
                d = handle.bulkRead(ep_in, 512, timeout=50)
                if len(d) <= 2:
                    break
        except usb1.USBErrorTimeout:
            pass

        transport = cls(ctx, handle, interface_index, ep_out, ep_in, 512)

        return transport

    def _sync_write_read(self, data: bytes, response_len: int) -> bytes:
        """Synchronous bulk write+read with FTDI modem status stripping."""
        self._handle.bulkWrite(self._ep_out, data, timeout=1000)

        if response_len == 0:
            return b""

        result = bytearray()
        while len(result) < response_len:
            packet = self._handle.bulkRead(self._ep_in, self._max_packet_size, timeout=1000)
            if len(packet) > 2:
                result.extend(packet[2:])

        return bytes(result[:response_len])

    async def write_read(self, data: bytes, response_len: int) -> bytes:
        """Send MPSSE commands and read response.

        FTDI prepends 2 modem status bytes to every USB read packet.
        This method strips them and accumulates data bytes until
        response_len bytes are collected.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._sync_write_read, data, response_len)

    async def close(self):
        """Reset FTDI bitmode and release device."""
        idx = self._interface_index + 1
        self._handle.controlWrite(
            VENDOR_OUT, SIO_SET_BITMODE, BITMODE_RESET << 8, idx, b'')
        self._handle.releaseInterface(self._interface_index)
        self._handle.close()
        self._ctx.close()
