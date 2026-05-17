from __future__ import annotations

import asyncio
import math
import time
import usb1
import ausb
from ausb.handle import BulkOutEndpoint, BulkInEndpoint, BulkPair
from ausb.exception import TransferTimeout, TransferOverflow, TransferStalled


# FTDI USB vendor requests
SIO_RESET = 0x00
SIO_SET_BAUDRATE = 0x03
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

# Fractional divisor encoding for FTDI baudrate generator.
# Maps sub-integer 0/8..7/8 to the 3-bit code in bits [16:14].
_FRAC_CODE = [0, 3, 2, 4, 1, 5, 6, 7]


def ftdi_baudrate_divisor(baudrate):
    """Compute FTDI baudrate divisor for non-H chips (48 MHz base).

    Returns (actual_baudrate, encoded_divisor).
    The actual baudrate never exceeds the requested baudrate.
    """
    if baudrate >= 3_000_000:
        return 3_000_000, 0
    if baudrate >= 2_000_000:
        return 2_000_000, 1
    # Divisor in 1/8ths, ceil to not exceed requested rate
    div8 = math.ceil(48_000_000 / baudrate)
    div8 = max(24, div8)  # integer part ≥ 3
    actual = 48_000_000 / div8
    integer = div8 >> 3
    frac = div8 & 7
    encoded = integer | (_FRAC_CODE[frac] << 14)
    return actual, encoded

class WriteOp:
    def __init__(self, blob):
        self.blob = blob
        self.future = asyncio.Future()

    async def run(self, ep, mps):
        for offset in range(0, len(self.blob), mps):
            await ep.write(self.blob[offset:offset + mps])
        self.future.set_result(None)

class ReadOp:
    def __init__(self, size, timeout = 1):
        self.size = size
        self.blob = b""
        self.future = asyncio.Future()
        self.timeout = timeout

    async def run(self, ep, mps, residual: bytes) -> bytes:
        """Resolve the future with exactly ``self.size`` payload bytes.

        ``residual`` is bytes left over from the previous ReadOp; we
        consume those first. Any payload pulled past ``self.size`` is
        returned to be carried over to the next ReadOp — without this,
        when the FTDI device bundles multiple batches' responses into
        one USB IN packet the next ReadOp will time out waiting for
        bytes that have already been read and discarded.
        """
        if len(residual) >= self.size:
            self.future.set_result(residual[:self.size])
            return residual[self.size:]

        self.blob = residual
        stall_count = 0
        deadline = time.monotonic() + self.timeout
        while len(self.blob) < self.size:
            if time.monotonic() > deadline:
                self.future.set_exception(TransferTimeout(
                    f"FTDI read timeout: got {len(self.blob)}/{self.size} "
                    f"payload bytes in {self.timeout}s"))
                return b""

            try:
                data = await ep.read(mps)
            except TransferTimeout:
                continue
            except TransferStalled as e:
                stall_count += 1
                if stall_count > 3:
                    self.future.set_exception(e)
                    return b""
                ep.resume()
                continue

            if len(data) <= 2:
                continue
            self.blob += data[2:]
            deadline = time.monotonic() + self.timeout

        leftover = self.blob[self.size:]
        self.blob = self.blob[:self.size]
        self.future.set_result(self.blob)
        return leftover

class FtdiTransport:
    """FTDI USB transport implementing the MPSSE Transport protocol.

    Uses ausb for USB access with run_in_executor for async bulk I/O.
    Handles FTDI-specific USB initialization (reset, purge, latency,
    MPSSE bitmode) and modem status byte stripping on reads.
    """

    def __init__(self, ctx, device, interface_index, pair, max_packet_size):
        self.__ctx = ctx
        self.__device = device
        self.__interface_index = interface_index
        self.__pair = pair
        self.__max_packet_size = max_packet_size
        self.__read_queue = asyncio.Queue()
        self.__write_queue = asyncio.Queue()
        self.__lock = asyncio.Lock()
        self.__reader = None
        self.__writer = None
        # Payload bytes pulled from the IN endpoint past the previous
        # ReadOp's requested size, carried over to the next ReadOp.
        # FTDI may bundle multiple batches' responses into a single USB
        # packet — without this carry-over, the bytes belonging to the
        # next ReadOp would be silently discarded.
        self.__read_residual = b""

        if ctx is not None:
            # Ctx-owning transports register a fallback close so the
            # USB device handle is released even if .close() never
            # runs (process exit, exception path).
            from ...lifecycle import on_shutdown
            on_shutdown(self.close)

    @staticmethod
    def __endpoint_mps(device, interface_index, ep_address):
        """Read endpoint max packet size from USB descriptor."""
        config = device.descriptor[device.configuration]
        setting = config[interface_index][0]
        ep = setting.endpoint_by_address(ep_address)
        return ep.max_packet_size

    @staticmethod
    def __drain(ep_in, mps):
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
        mps = cls.__endpoint_mps(device, interface_index, ep_in_addr)

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)
        pair = BulkPair(ep_out, ep_in)

        cls.__drain(ep_in, mps)

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
        mps = cls.__endpoint_mps(device, interface_index, ep_in_addr)

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)
        pair = BulkPair(ep_out, ep_in)

        cls.__drain(ep_in, mps)

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
        mps = cls.__endpoint_mps(device, interface_index, ep_in_addr)

        ep_out = BulkOutEndpoint(device, ep_out_addr, mps)
        ep_in = BulkInEndpoint(device, ep_in_addr, mps)
        pair = BulkPair(ep_out, ep_in)

        cls.__drain(ep_in, mps)

        return cls(None, device, interface_index, pair, mps)

    async def set_baudrate(self, baudrate):
        """Set FTDI baud rate. Returns actual baudrate (never exceeds requested).

        Uses non-H encoding (48 MHz base). Suitable for FT232R, FT-X series.
        """
        actual, encoded = ftdi_baudrate_divisor(baudrate)
        value = encoded & 0xFFFF
        index = (encoded >> 16) & 0xFFFF
        if self.__interface_index > 0:
            index = (index & 0xFF00) | (self.__interface_index + 1)
        await self.__device.vendor_control(SIO_SET_BAUDRATE, value, index, b'')
        return actual

    # Write chunk size.  libusb splits into USB packets, so this only
    # needs to stay within the host-controller's transfer limits.
    __WRITE_CHUNK = 65536

    def write(self, data: bytes):
        """Write data to the FTDI bulk OUT endpoint."""
        w = WriteOp(data)
        self.__write_queue.put_nowait(w)
        if self.__writer is None:
            self.__writer = asyncio.create_task(self.__writer_worker())
        return w.future

    async def __writer_worker(self):
        while True:
            async with self.__lock:
                try:
                    todo = self.__write_queue.get_nowait()
                except asyncio.QueueEmpty:
                    self.__writer = None
                    return
            await todo.run(self.__pair.out, self.__max_packet_size)

    # Maximum time to spend in read() before raising a timeout.
    __READ_DEADLINE = 1.0

    def read(self, size: int, timeout: float|None = None):
        """Read data from the FTDI bulk IN endpoint."""
        if timeout is None:
            timeout = self.__READ_DEADLINE
        r = ReadOp(size, timeout = timeout)
        self.__read_queue.put_nowait(r)
        if self.__reader is None:
            self.__reader = asyncio.create_task(self.__reader_worker())
        return r.future

    async def __reader_worker(self):
        while True:
            async with self.__lock:
                try:
                    todo = self.__read_queue.get_nowait()
                except asyncio.QueueEmpty:
                    self.__reader = None
                    return
            self.__read_residual = await todo.run(
                self.__pair.in_, self.__max_packet_size, self.__read_residual)

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

    async def close(self):
        """Reset FTDI bitmode and release device.

        When created via from_device() (ctx=None), only releases the
        interface — the caller owns the device lifetime.
        """
        from ...lifecycle import cancel_shutdown
        async with self.__lock:
            while not self.__read_queue.empty():
                self.__read_queue.get_nowait().future.set_exception(asyncio.CancelledError())
            while not self.__write_queue.empty():
                self.__write_queue.get_nowait().future.set_exception(asyncio.CancelledError())
        if x := self.__reader:
            await x
        if x := self.__writer:
            await x
        cancel_shutdown(self.close)
        idx = self.__interface_index + 1
        await self.__device.vendor_control(
            SIO_SET_BITMODE, BITMODE_RESET << 8, idx, b'')
        self.__device.handle.releaseInterface(self.__interface_index)
        if self.__ctx is not None:
            self.__device.handle.close()
            self.__ctx.close()
            self.__ctx = None
