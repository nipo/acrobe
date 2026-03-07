from __future__ import annotations

import asyncio
import time
import usb1
import ausb
from ausb.handle import BulkOutEndpoint, BulkInEndpoint, BulkPair
from ausb.exception import TransferTimeout, TransferOverflow, TransferStalled


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
BITMODE_SYNCBB = 0x04


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

    @staticmethod
    def _endpoint_mps(device, interface_index, ep_address):
        """Read endpoint max packet size from USB descriptor."""
        config = device.descriptor[device.configuration]
        setting = config[interface_index][0]
        ep = setting.endpoint_by_address(ep_address)
        return ep.max_packet_size

    @staticmethod
    def _drain(ep_in, mps):
        """Drain stale data from an FTDI IN endpoint.

        Reads until only modem status bytes remain.  Handles overflow
        on Full-Speed devices by doubling the read buffer, and clears
        stalled endpoints that may occur after a device reset.
        """
        drain_buf = mps
        try:
            while True:
                try:
                    d = ep_in.read_sync(drain_buf, timeout=50)
                except TransferOverflow:
                    drain_buf *= 2
                    continue
                except TransferStalled:
                    ep_in.resume()
                    continue
                if len(d) <= 2:
                    break
        except TransferTimeout:
            pass

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
        mps = cls._endpoint_mps(device, interface_index, ep_in_addr)

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)
        pair = BulkPair(ep_out, ep_in)

        cls._drain(ep_in, mps)

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
        mps = cls._endpoint_mps(device, interface_index, ep_in_addr)

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)
        pair = BulkPair(ep_out, ep_in)

        cls._drain(ep_in, mps)

        return cls(None, device, interface_index, pair, mps)

    @classmethod
    async def from_device_bitbang(cls, device, interface_index=0, oe_mask=0):
        """Initialize sync bitbang mode on an already-opened ausb Device.

        Like from_device(), but sets SYNCBB bitmode instead of MPSSE.
        oe_mask sets which pins are outputs (1=output, 0=input).
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
        await device.vendor_control(
            SIO_SET_BITMODE, (BITMODE_SYNCBB << 8) | oe_mask, idx, b'')

        ep_out_addr = 0x02 + interface_index * 2
        ep_in_addr = 0x81 + interface_index * 2
        mps = cls._endpoint_mps(device, interface_index, ep_in_addr)

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)
        pair = BulkPair(ep_out, ep_in)

        cls._drain(ep_in, mps)

        return cls(None, device, interface_index, pair, mps)

    # Write chunk size.  libusb splits into USB packets, so this only
    # needs to stay within the host-controller's transfer limits.
    _WRITE_CHUNK = 65536

    async def write(self, data: bytes):
        """Write data to the FTDI bulk OUT endpoint."""
        for offset in range(0, len(data), self._WRITE_CHUNK):
            await self._pair.out.write(data[offset:offset + self._WRITE_CHUNK])

    async def transfer(self, data: bytes, byte_count: int) -> bytes:
        """Write data and read byte_count bytes concurrently.

        Starts both write and read simultaneously so the host has an IN
        transfer pending while the device processes commands.  This is
        required for devices (like the Sipeed MCU) with small response
        buffers that need the host to drain data during processing.
        """
        _, rsp = await asyncio.gather(
            self.write(data),
            self.read(byte_count))
        return rsp

    # Maximum time to spend in read() before raising a timeout.
    _READ_DEADLINE = 5.0

    async def read(self, byte_count: int) -> bytes:
        """Read byte_count bytes from the FTDI bulk IN endpoint.

        Strips the 2 modem status bytes that FTDI prepends to every
        USB IN packet, accumulating payload bytes until byte_count
        bytes are collected.

        Handles transient USB conditions:
        - Timeout: retried (normal when read starts before device
          has data, e.g. during concurrent write+read)
        - Overflow: doubles read buffer and retries
        - Stall: clears halt and retries (max 3 times)

        Raises TransferTimeout if no progress after _READ_DEADLINE
        seconds, preventing infinite loops from modem-only packets.
        """
        mps = self._max_packet_size
        read_size = mps
        stall_count = 0
        deadline = time.monotonic() + self._READ_DEADLINE
        result = bytearray()
        while len(result) < byte_count:
            if time.monotonic() > deadline:
                raise TransferTimeout(
                    f"FTDI read timeout: got {len(result)}/{byte_count} "
                    f"payload bytes in {self._READ_DEADLINE}s")
            try:
                data = await self._pair.in_.read(read_size)
            except TransferTimeout:
                continue
            except TransferOverflow:
                read_size *= 2
                continue
            except TransferStalled:
                stall_count += 1
                if stall_count > 3:
                    raise
                self._pair.in_.resume()
                continue
            for offset in range(0, len(data), mps):
                chunk = data[offset:offset + mps]
                if len(chunk) > 2:
                    result.extend(chunk[2:])
        return bytes(result[:byte_count])

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
