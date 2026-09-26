import asyncio
from collections import deque

from ..adapter.model import Adapter, AdapterInfo, adapter_db
from ..db import NoMatch
from ..lifecycle import cancel_shutdown, on_shutdown
from ..protocol.datagram import Datagram, Recv, Send

class UsbFramed(Datagram):
    READ_PACKETS = 64
    FRAME_LIMIT = 4096
    TIMEOUT_MS = 5000
    DRAIN_MS = 20

    def __init__(self, ep_out, ep_in, mps, name="usb"):
        super().__init__(name)
        self.ep_out = ep_out
        self.ep_in = ep_in
        self.mps = mps
        self.read_size = self.READ_PACKETS * mps
        self.__sends = deque()
        self.__recvs = deque()
        self.__worker = None

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                self.__sends.append((op.data, future))
            elif isinstance(op, Recv):
                self.__recvs.append(future)
            else:
                raise TypeError(
                    f"{type(self).__name__} carries datagrams, not "
                    f"{type(op).__name__}")
        if self.__sends and self.__recvs:
            if self.__worker is None or self.__worker.done():
                self.__worker = asyncio.create_task(self.__worker_loop())

    async def __worker_loop(self):
        # Firmware disarms OUT until the IN reply is consumed. Each logical
        # Send/Recv pair may need several bounded USB command/reply frames.
        while self.__sends and self.__recvs:
            data, send_future = self.__sends.popleft()
            recv_future = self.__recvs.popleft()
            try:
                response = await self.__exchange(data)
            except BaseException as exc:  # noqa: BLE001 -- forward to caller
                self.__fail(send_future, exc)
                self.__fail(recv_future, exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                continue
            if send_future is not None and not send_future.done():
                send_future.set_result(None)
            if recv_future is not None and not recv_future.done():
                recv_future.set_result((response, None))

    @staticmethod
    def __command_size(data, pos):
        opcode = data[pos]
        if not opcode & 0x80:
            count = (opcode & 0x1f) + 1
            return (count if opcode & 0x40 else 0,
                    1 + (count if opcode & 0x20 else 0))
        if opcode & 0xe0 == 0xe0:
            return (1 if opcode & 0x10 else 0,
                    1 + (1 if opcode & 0x08 else 0))
        if opcode in (0x83, 0x87):
            return 1, 1
        if opcode in (0x80, 0x81, 0x82, 0x84, 0x85) or \
                opcode & 0xf0 in (0xa0, 0xb0) or \
                opcode & 0xf8 in (0x90, 0x98):
            return 0, 1
        raise ValueError(f"Unknown NSL JTAG opcode {opcode:#04x}")

    def split_commands(self, data):
        """Preflight the complete stream, splitting at command boundaries.

        Bound *both* directions: a read-only 32-byte shift has one command
        byte but replies with 33 bytes. The firmware preflights each USB
        frame independently and retains TAP state between them.
        """
        frames = []
        start = pos = reply_size = 0
        while pos < len(data):
            payload, reply = self.__command_size(data, pos)
            next_pos = pos + payload + 1
            if next_pos > len(data):
                raise ValueError("Truncated NSL JTAG command")
            if (next_pos - start > self.FRAME_LIMIT or
                    reply_size + reply > self.FRAME_LIMIT) and pos > start:
                frames.append((bytes(data[start:pos]), reply_size))
                start = pos
                reply_size = 0
            if next_pos - start > self.FRAME_LIMIT or reply_size + reply > self.FRAME_LIMIT:
                raise ValueError("NSL JTAG command exceeds the USB frame limit")
            reply_size += reply
            pos = next_pos
        frames.append((bytes(data[start:]), reply_size))
        return frames

    async def __exchange(self, data):
        response = bytearray()
        frames = self.split_commands(data)
        if len(frames) > 1:
            self.logger.note("NSL USB transaction %d bytes in %d frames", len(data), len(frames))
        for frame, expected_size in frames:
            await self.__write_frame(frame)
            reply = await self.__read_frame()
            if len(reply) == 2 and reply[0] == 0xfa:
                raise IOError(f"BL616 NSL command rejected: {reply[1]}")
            if len(reply) != expected_size:
                raise IOError(f"BL616 NSL response: {len(reply)} bytes, "
                              f"expected {expected_size}")
            command_pos = response_pos = 0
            while command_pos < len(frame):
                payload, reply_size = self.__command_size(frame, command_pos)
                response_pos += reply_size
                if reply[response_pos - 1] != 0x5a:
                    raise IOError("BL616 NSL command acknowledgment missing")
                command_pos += payload + 1
            response += reply
        return bytes(response)

    @staticmethod
    def __fail(future, exc):
        if future is not None and not future.done():
            future.set_exception(exc)

    async def __write_frame(self, data):
        """One datagram out. A frame that is a whole number of packets ends on
        a zero-length one: without it the device is still waiting for the rest
        of the frame. An empty frame is that zero-length packet and nothing
        else -- a second one would be a second empty frame."""
        await self.ep_out.write(bytes(data), timeout=self.TIMEOUT_MS)
        if data and len(data) % self.mps == 0:
            await self.ep_out.write(b"", timeout=self.TIMEOUT_MS)

    async def __read_frame(self):
        """One datagram in, gathered until the short packet that ends it."""
        frame = bytearray()
        while True:
            chunk = await self.ep_in.read(self.read_size,
                                          timeout=self.TIMEOUT_MS)
            frame += chunk
            if len(chunk) < self.read_size:
                return bytes(frame)

    def drain(self):
        """Discard whatever the endpoint still holds from a previous session.

        A host that died between a command and its reply leaves the reply in
        the device; read it now and the frames would be one apart for the rest
        of the connection."""
        from ausb.exception import TransferTimeout
        try:
            while True:
                if not self.ep_in.read_sync(self.read_size,
                                            timeout=self.DRAIN_MS):
                    return
        except TransferTimeout:
            return

    async def stop(self):
        if self.__worker is not None:
            self.__worker.cancel()
            try:
                await self.__worker
            except asyncio.CancelledError:
                pass
            self.__worker = None
        error = RuntimeError("USB JTAG adapter closed")
        for _, future in self.__sends:
            self.__fail(future, error)
        for future in self.__recvs:
            self.__fail(future, error)
        self.__sends.clear()
        self.__recvs.clear()


@adapter_db.register(AdapterInfo("TC60K", vid=0x1500, pid=0xde55))
class UsbAdapter(Adapter):
    VENDOR_CLASS = 0xFF
    BULK_ATTRIBUTE = 2
    JTAG_CLOCK_BASE = "40M"

    def __init__(self, name, info=None, descriptor=None):
        super().__init__(name, info, descriptor)
        self.device = None
        self.datagram = None
        self.__interface = None

    def child_hints(self):
        return ["jtag"]

    async def child_spawn(self, name):
        if name == "jtag":
            await self.__ensure_open()
            from acrobe.component.nsl.transactor.jtag import JtagInterface
            interface = JtagInterface(self.datagram, name="jtag")
            interface.option_set("fin", self.JTAG_CLOCK_BASE)
            interface.freq_cap("tc60k_verified", 10e6)
            return interface
        raise NoMatch("interface", name)

    async def __ensure_open(self):
        if self.datagram is not None:
            return
        from ausb.handle import BulkInEndpoint, BulkOutEndpoint
        device = self.descriptor.open()
        interface, out_address, in_address, mps = self.__find_interface(device)
        self.__release_kernel(device, interface)
        device.handle.claimInterface(interface)
        datagram = UsbFramed(BulkOutEndpoint(device, out_address, mps),
                             BulkInEndpoint(device, in_address, mps), mps)
        datagram.drain()
        self.device = device
        self.__interface = interface
        self.datagram = datagram
        on_shutdown(self.close)

    @staticmethod
    def __release_kernel(device, interface):
        import usb1
        try:
            device.handle.detachKernelDriver(interface)
        except (usb1.USBErrorNotFound, usb1.USBErrorNotSupported,
                usb1.USBErrorAccess):
            pass

    @classmethod
    def __find_interface(cls, device):
        """The vendor-defined interface and its bulk pair, as
        ``(interface, out address, in address, packet size)``. The device
        exposes exactly one, but it is looked up rather than assumed: the
        interface number is not part of what the vendor and product ids
        promise."""
        configuration = device.descriptor[device.configuration]
        for index, interface in enumerate(configuration):
            setting = interface[0]
            if setting.classes[0] != cls.VENDOR_CLASS:
                continue
            found = cls.__bulk_pair(setting)
            if found is not None:
                return (index,) + found
        raise IOError(
            "no vendor-defined interface with a bulk endpoint pair")

    @classmethod
    def __bulk_pair(cls, setting):
        out_address = in_address = None
        mps = 0
        for endpoint in setting:
            if (endpoint.attributes & 0x3) != cls.BULK_ATTRIBUTE:
                continue
            if endpoint.address & 0x80:
                if in_address is None:
                    in_address = endpoint.address
                    mps = max(mps, endpoint.max_packet_size)
            elif out_address is None:
                out_address = endpoint.address
                mps = max(mps, endpoint.max_packet_size)
        if out_address is None or in_address is None:
            return None
        return out_address, in_address, mps

    async def close(self):
        if self.datagram is None:
            return
        cancel_shutdown(self.close)
        await self.datagram.stop()
        try:
            self.device.handle.releaseInterface(self.__interface)
        finally:
            self.device.handle.close()
        self.datagram = None
        self.device = None
        self.__interface = None
