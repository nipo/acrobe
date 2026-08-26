"""Linux `stream_endpoint` character device as a :class:`Datagram`.

The `stream_endpoint` kernel driver binds the `nsl_amba`
`axi4_stream_endpoint_lite` IP block -- a pair of AXI4-Stream
interfaces mapped onto an AXI-Lite register set -- and exposes each
instance as `/dev/<label>`, where `label` comes from the device-tree
node.

Its character-device ABI is framed: every `read()` / `write()` carries
a 4-byte `struct stream_endpoint_chunk` header followed by the chunk
payload, and the `LAST` flag marks the chunk that terminates a frame.
That maps onto :class:`acrobe.protocol.datagram.Datagram` directly: one
`Send` is one frame, one `Recv` is one frame.

Path syntax::

    stream-<label>/datagram/...

so the NSL transactors and bnoc wrappers registered on
:attr:`Datagram.db` (`nsl_jtag`, `nsl_swd`, `nsl_spi`, `committed`,
`routed`) stack on top with no further glue.

Padding: the IP block moves `stream_width` payload bytes per beat and
the driver zero-pads the beat that completes a frame, so a peer
observes frame lengths rounded up to a multiple of `stream_width`. A
received frame may therefore carry up to `stream_width - 1` trailing
pad bytes; this layer cannot tell them from payload, the protocol above
knows its own lengths.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import glob
import os
import struct
import sys
from collections import deque
from dataclasses import dataclass

from ..db import NoMatch
from ..lifecycle import cancel_shutdown, on_shutdown
from ..protocol.datagram import Datagram, Recv, Send
from .model import Adapter, Enumerator, enumerator_db

if sys.platform != "linux":
    raise ImportError(
        f"acrobe.adapter.stream_endpoint not supported on {sys.platform}")


SYSFS_DRIVER_DIR = "/sys/bus/platform/drivers/stream_endpoint"


class Chunk:
    """The `struct stream_endpoint_chunk` wire header.

    From `<stream_endpoint.h>`: `__u16 size` then `__u16 flags`, host
    byte order, followed by `size` payload bytes.
    """

    HEADER = struct.Struct("=HH")
    SIZE = HEADER.size

    LAST = 1 << 0

    @classmethod
    def pack(cls, payload: bytes, last: bool) -> bytes:
        return cls.HEADER.pack(len(payload), cls.LAST if last else 0) + payload

    @classmethod
    def unpack(cls, raw: bytes) -> tuple[bytes, bool]:
        """Split one `read()` return value into `(payload, last)`."""
        if len(raw) < cls.SIZE:
            raise ValueError(
                f"short chunk: {len(raw)} bytes, header needs {cls.SIZE}")
        size, flags = cls.HEADER.unpack_from(raw)
        payload = raw[cls.SIZE:]
        if len(payload) != size:
            raise ValueError(
                f"chunk header claims {size} payload bytes, got {len(payload)}")
        if flags & ~cls.LAST:
            raise ValueError(f"chunk carries reserved flags {flags:#06x}")
        return payload, bool(flags & cls.LAST)


@dataclass(frozen=True, slots=True)
class StreamEndpointInfo:
    """Endpoint geometry, from the `STREAM_ENDPOINT_GET_INFO` ioctl."""

    stream_width: int
    chunk_max: int
    rx_hw_depth: int
    tx_hw_depth: int
    rx_buffer_size: int
    tx_buffer_size: int

    STRUCT = struct.Struct("=6I")
    # _IOR(STREAM_ENDPOINT_IOC_MAGIC=0xea, 0x00, struct stream_endpoint_info):
    # dir=_IOC_READ(2)<<30 | size(24)<<16 | type(0xea)<<8 | nr(0x00)
    GET_INFO = 0x8018EA00

    @classmethod
    def read(cls, fd: int) -> "StreamEndpointInfo":
        raw = fcntl.ioctl(fd, cls.GET_INFO, b"\x00" * cls.STRUCT.size)
        return cls(*cls.STRUCT.unpack(raw))

    def as_metadata(self) -> dict:
        return {
            "stream_width": self.stream_width,
            "chunk_max": self.chunk_max,
            "rx_hw_depth": self.rx_hw_depth,
            "tx_hw_depth": self.tx_hw_depth,
            "rx_buffer_size": self.rx_buffer_size,
            "tx_buffer_size": self.tx_buffer_size,
        }


class StreamEndpointDatagram(Datagram):
    """Framed transport over one `/dev/<label>` stream endpoint.

    The fd is non-blocking and driven from the event loop:
    `loop.add_reader` pulls chunks and reassembles frames, `add_writer`
    unblocks a transmit that filled the driver's buffer.
    """

    # Buffered complete frames past which the reader is disarmed, so
    # the driver's own buffer fills and backpressure reaches the AXI
    # stream instead of being absorbed by unbounded Python buffering.
    RX_FRAME_HIGH_WATER = 64

    def __init__(self, path: str, name: str = "datagram"):
        super().__init__(name)
        self.__path = path
        self.__fd: int | None = None
        self.__loop: asyncio.AbstractEventLoop | None = None
        self.__info: StreamEndpointInfo | None = None
        self.__reader_armed = False
        # Reassembly of the frame currently arriving.
        self.__partial = bytearray()
        # Complete frames not yet claimed by a Recv, and Recv futures
        # not yet fed a frame. At most one of the two is non-empty.
        self.__rx_frames: deque[bytes] = deque()
        self.__recv_waiters: deque = deque()
        self.__tx_queue: deque = deque()
        self.__tx_task: asyncio.Task | None = None

    @property
    def path(self) -> str:
        return self.__path

    @property
    def rx_backlog(self) -> int:
        """Complete frames received but not yet claimed by a `Recv`.
        Capped at :attr:`RX_FRAME_HIGH_WATER` while the reader is
        disarmed."""
        return len(self.__rx_frames)

    @property
    def info(self) -> StreamEndpointInfo:
        if self.__info is None:
            raise RuntimeError(f"{self.path} not started")
        return self.__info

    async def start(self):
        fd = os.open(self.__path, os.O_RDWR | os.O_NONBLOCK)
        self.__fd = fd
        self.__loop = asyncio.get_running_loop()
        try:
            self.__info = StreamEndpointInfo.read(fd)
        except OSError:
            self.__teardown()
            raise
        self.metadata.update(self.__info.as_metadata())
        self.metadata["path"] = self.__path
        self.logger.note(
            "%s: %d byte stream, chunk max %d, hw fifos %d/%d beats in/out",
            self.__path, self.__info.stream_width, self.__info.chunk_max,
            self.__info.rx_hw_depth, self.__info.tx_hw_depth)
        self.__rearm_reader()
        on_shutdown(self.stop)

    async def stop(self):
        cancel_shutdown(self.stop)
        task = self.__tx_task
        self.__tx_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.__teardown()

    def __teardown(self):
        """Release the fd and put the endpoint in a permanent closed
        state. Idempotent; also the hard-error path (driver unbound,
        module unloaded), so that further ops fail fast on the
        `__fd is None` guard rather than looping on a dead handle."""
        if self.__loop is not None and self.__fd is not None:
            for remove in (self.__loop.remove_reader, self.__loop.remove_writer):
                try:
                    remove(self.__fd)
                except Exception:
                    pass
        self.__reader_armed = False
        if self.__fd is not None:
            try:
                os.close(self.__fd)
            except OSError:
                pass
            self.__fd = None
        self.__fail_waiters(EOFError(f"{self.__path} closed"))

    def __fail_waiters(self, exc):
        while self.__recv_waiters:
            future = self.__recv_waiters.popleft()
            if future is not None and not future.done():
                future.set_exception(exc)
        while self.__tx_queue:
            _data, future = self.__tx_queue.popleft()
            if future is not None and not future.done():
                future.set_exception(exc)

    # ------------------------------------------------------------------
    # Batcher
    # ------------------------------------------------------------------

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                if not op.data:
                    # The LAST flag rides a beat, so a frame needs at
                    # least one payload byte; the driver answers EINVAL.
                    if future is not None and not future.done():
                        future.set_exception(
                            ValueError("stream endpoint: empty frame"))
                    continue
                self.__tx_queue.append((op.data, future))
                self.__ensure_tx()
            elif isinstance(op, Recv):
                self.__recv_waiters.append(future)
                self.__dispatch_rx()
            else:
                if future is not None and not future.done():
                    future.set_exception(TypeError(
                        f"{type(self).__name__}: unsupported op "
                        f"{type(op).__name__}"))

    # ------------------------------------------------------------------
    # Receive
    # ------------------------------------------------------------------

    def __on_readable(self):
        if self.__fd is None:
            return
        want = Chunk.SIZE + self.info.chunk_max
        while True:
            try:
                raw = os.read(self.__fd, want)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                self.__teardown()
                return
            if not raw:
                self.__teardown()
                return
            try:
                payload, last = Chunk.unpack(raw)
            except ValueError as e:
                self.logger.error("%s: %s", self.__path, e)
                self.__teardown()
                return
            self.__partial += payload
            if last:
                self.__rx_frames.append(bytes(self.__partial))
                self.__partial.clear()
                if len(self.__rx_frames) >= self.RX_FRAME_HIGH_WATER:
                    break
        self.__dispatch_rx()

    def __dispatch_rx(self):
        """Hand buffered frames to waiting Recv futures, oldest first,
        then reconcile the reader's armed state."""
        while self.__rx_frames and self.__recv_waiters:
            future = self.__recv_waiters.popleft()
            frame = self.__rx_frames.popleft()
            if future is not None and not future.done():
                future.set_result((frame, None))
        self.__rearm_reader()

    def __rearm_reader(self):
        if self.__fd is None or self.__loop is None:
            return
        want = len(self.__rx_frames) < self.RX_FRAME_HIGH_WATER
        if want == self.__reader_armed:
            return
        if want:
            self.__loop.add_reader(self.__fd, self.__on_readable)
        else:
            self.__loop.remove_reader(self.__fd)
        self.__reader_armed = want

    # ------------------------------------------------------------------
    # Transmit
    # ------------------------------------------------------------------

    def __ensure_tx(self):
        if self.__tx_task is None or self.__tx_task.done():
            self.__tx_task = asyncio.ensure_future(self.__tx_loop())

    async def __tx_loop(self):
        """Drain the transmit queue one frame at a time. A single task
        owns the fd's write side, so chunks of concurrently-posted
        frames can never interleave on the wire."""
        while self.__tx_queue:
            data, future = self.__tx_queue.popleft()
            try:
                await self.__send_frame(data)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
                continue
            if future is not None and not future.done():
                future.set_result(None)

    async def __send_frame(self, data: bytes):
        chunk_max = self.info.chunk_max
        offset = 0
        while offset < len(data):
            end = min(offset + chunk_max, len(data))
            written = await self.__send_chunk(data[offset:end], last=end == len(data))
            offset += written

    async def __send_chunk(self, payload: bytes, *, last: bool) -> int:
        """Submit one chunk, retrying a short write. Returns the number
        of payload bytes accepted -- always `len(payload)`, since a
        short write leaves the LAST flag unapplied and is resubmitted
        from where it stopped."""
        offset = 0
        while offset < len(payload):
            if self.__fd is None:
                raise EOFError(f"{self.__path} closed")
            frame = Chunk.pack(payload[offset:], last)
            try:
                written = os.write(self.__fd, frame)
            except BlockingIOError:
                await self.__writable()
                continue
            except InterruptedError:
                continue
            except OSError as e:
                self.__teardown()
                raise EOFError(f"{self.__path} write failed: {e}") from e
            offset += written - Chunk.SIZE
        return offset

    async def __writable(self):
        future = self.__loop.create_future()
        self.__loop.add_writer(
            self.__fd, lambda: future.done() or future.set_result(None))
        try:
            await future
        finally:
            if self.__fd is not None:
                self.__loop.remove_writer(self.__fd)


class StreamEndpointAdapter(Adapter):
    """One `stream_endpoint` character device."""

    def __init__(self, name: str, path: str):
        super().__init__(name)
        self.__path = path

    @property
    def ident(self):
        return self.__path

    def child_hints(self):
        return ["datagram"]

    async def child_spawn(self, name):
        if name.lower() == "datagram":
            return StreamEndpointDatagram(self.__path, name="datagram")
        raise NoMatch("interface", name)

@enumerator_db.register("stream-endpoint")
class StreamEndpointEnumerator(Enumerator):
    """Scans the platform bus for `stream_endpoint` devices and attaches
    one `StreamEndpointAdapter` per bound endpoint."""

    def __init__(self, driver_dir: str = SYSFS_DRIVER_DIR):
        self.__driver_dir = driver_dir

    def labels(self) -> list[str]:
        """Character-device names of every bound endpoint.

        The driver registers a misc device named after the mandatory
        device-tree `label`, so `<device>/misc/<label>` is both the
        label and the `/dev` node name -- no property parsing and no
        major/minor resolution needed."""
        pattern = os.path.join(self.__driver_dir, "*", "misc", "*")
        return sorted(os.path.basename(p) for p in glob.glob(pattern))

    async def populate(self, hw_root):
        for label in self.labels():
            name = f"stream-{label}"
            if hw_root.has_child(name):
                continue
            hw_root.child_add(StreamEndpointAdapter(name, f"/dev/{label}"))
