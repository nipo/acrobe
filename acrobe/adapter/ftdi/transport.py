from __future__ import annotations

import asyncio
import usb1
import ausb
from ausb.handle import BulkOutEndpoint, BulkInEndpoint, BulkPair
from ausb.exception import TransferTimeout


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


class FtdiTransport:
    """FTDI USB transport implementing the MPSSE Transport protocol.

    Uses ausb for USB access with run_in_executor for async bulk I/O.
    Handles FTDI-specific USB initialization (reset, purge, latency,
    MPSSE bitmode) and modem status byte stripping on reads.
    """

    def __init__(self, ctx, device, interface_index, pair, max_packet_size):
        self._ctx = ctx
        self._device = device
        self._interface_index = interface_index
        self._pair = pair
        self._max_packet_size = max_packet_size

    @classmethod
    async def open(cls, *, vid, pid, interface_index=0):
        """Open an FTDI device and initialize MPSSE mode.

        Args:
            vid: USB Vendor ID
            pid: USB Product ID
            interface_index: USB interface index (0=A, 1=B)
        """
        ctx = ausb.Context(enable_hotplug=False)
        dev_desc = ctx.device_get(vendor_id=vid, product_id=pid)
        device = dev_desc.open()

        try:
            device.handle.detachKernelDriver(interface_index)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported, usb1.USBErrorAccess):
            pass

        device.handle.claimInterface(interface_index)

        # FTDI uses 1-based interface index in control request index field
        idx = interface_index + 1

        await device.vendor_control(SIO_RESET, SIO_RESET_SIO, idx, b'')
        await device.vendor_control(SIO_SET_LATENCY_TIMER, 1, idx, b'')
        await device.vendor_control(SIO_SET_BITMODE, BITMODE_RESET << 8, idx, b'')
        await device.vendor_control(SIO_RESET, SIO_RESET_PURGE_RX, idx, b'')
        await device.vendor_control(SIO_RESET, SIO_RESET_PURGE_TX, idx, b'')
        await device.vendor_control(SIO_SET_BITMODE, BITMODE_MPSSE << 8, idx, b'')

        # FTDI endpoint addresses: chan A = OUT 0x02/IN 0x81, chan B = OUT 0x04/IN 0x83
        ep_out_addr = 0x02 + interface_index * 2
        ep_in_addr = 0x81 + interface_index * 2
        mps = 512

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)
        pair = BulkPair(ep_out, ep_in)

        # Drain stale data from FTDI buffers
        try:
            while True:
                d = ep_in.read_sync(mps, timeout=50)
                if len(d) <= 2:
                    break
        except TransferTimeout:
            pass

        return cls(ctx, device, interface_index, pair, mps)

    @classmethod
    async def from_device(cls, device, interface_index=0):
        """Initialize MPSSE on an already-opened ausb Device.

        Like open(), but skips Context creation and device lookup.
        The caller owns the Context lifetime.
        """
        try:
            device.handle.detachKernelDriver(interface_index)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported, usb1.USBErrorAccess):
            pass

        device.handle.claimInterface(interface_index)

        idx = interface_index + 1

        await device.vendor_control(SIO_RESET, SIO_RESET_SIO, idx, b'')
        await device.vendor_control(SIO_SET_LATENCY_TIMER, 1, idx, b'')
        await device.vendor_control(SIO_SET_BITMODE, BITMODE_RESET << 8, idx, b'')
        await device.vendor_control(SIO_RESET, SIO_RESET_PURGE_RX, idx, b'')
        await device.vendor_control(SIO_RESET, SIO_RESET_PURGE_TX, idx, b'')
        await device.vendor_control(SIO_SET_BITMODE, BITMODE_MPSSE << 8, idx, b'')

        ep_out_addr = 0x02 + interface_index * 2
        ep_in_addr = 0x81 + interface_index * 2
        mps = 512

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)
        pair = BulkPair(ep_out, ep_in)

        try:
            while True:
                d = ep_in.read_sync(mps, timeout=50)
                if len(d) <= 2:
                    break
        except TransferTimeout:
            pass

        return cls(None, device, interface_index, pair, mps)

    def _sync_write_read(self, data: bytes, response_len: int) -> bytes:
        """Synchronous bulk write+read with FTDI modem status stripping."""
        mps = self._max_packet_size
        for offset in range(0, len(data), mps):
            self._pair.out.write_sync(data[offset:offset + mps])

        if response_len == 0:
            return b""

        result = bytearray()
        while len(result) < response_len:
            packet = self._pair.in_.read_sync(self._max_packet_size)
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
        """Reset FTDI bitmode and release device.

        When created via from_device() (ctx=None), only releases the
        interface — the caller owns the device lifetime.
        """
        idx = self._interface_index + 1
        await self._device.vendor_control(
            SIO_SET_BITMODE, BITMODE_RESET << 8, idx, b'')
        self._device.handle.releaseInterface(self._interface_index)
        if self._ctx is not None:
            self._device.handle.close()
            self._ctx.close()
