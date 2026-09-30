"""NSL AXI4 memory-mapped bus tunneled over a datagram channel.

Host side of NSL ``nsl_amba.mm_stream_adapter.axi4_mm_on_stream``. The
RTL serializes the five AXI4 channels of a bus on an AXI4-Stream; this
component plays the remote AXI master: it emits AW / W / AR frames that
the RTL replays on its ``master_o`` port, and decodes the B / R frames
it returns. The local memory-access surface is
:mod:`acrobe.protocol.memory` (bulk family native, register family
served from it).

Path syntax::

    udp/<host>:<port>/nsl_axi4_mm(<option>=<value>,...)

Wire format
-----------

One datagram carries one stream frame. The first byte is the channel
the frame belongs to — the stream TID the RTL funnel/dispatcher route
on, serialized as a prefix byte by ``axi4_stream_meta_packer`` /
``axi4_stream_meta_unpacker`` (``meta_elements_c => "i"``) with a
3-bit stream TID:

====  =======  =========
Byte  Channel  Direction
====  =======  =========
0x00  B        device to host
0x01  AW       host to device
0x02  AR       host to device
0x03  R        device to host
0x04  W        host to device
====  =======  =========

The rest of the frame is a sequence of channel beats. Each beat is a
bit vector, packed LSB first in the field order below, zero-padded to a
whole number of bytes and sent least significant byte first. Optional
fields are absent (zero width) when the RTL configuration omits them.

* AW / AR, one beat per frame: ``addr[address_width]``, ``prot[3]``,
  ``size[3]`` (if ``size``), ``burst[2]`` (if ``burst`` and
  ``max_length > 1``), ``cache[4]`` (if ``cache``),
  ``len[log2(max_length)]`` (beat count - 1), ``lock[1]`` (if
  ``lock``), ``id[id_width]``, ``qos[4]`` (if ``qos``), ``region[4]``
  (if ``region``), ``user[user_width]``.
* W, one frame per burst, ``len + 1`` beats; the end of the frame is
  WLAST: ``data[8 * bytes]`` (lane 0 first), ``strb[bytes]`` (bit i
  enables lane i), ``user[user_width]``.
* B, one beat per frame: ``resp[2]``, ``id[id_width]``,
  ``user[user_width]``.
* R, one frame per burst, ``len + 1`` beats; the end of the frame is
  RLAST: ``data[8 * bytes]``, ``resp[2]``, ``id[id_width]``,
  ``user[user_width]``.

``resp`` is OKAY=0, EXOKAY=1, SLVERR=2, DECERR=3; ``burst`` is
FIXED=0, INCR=1, WRAP=2.

The RTL dispatcher forwards one frame at a time, so a frame whose
channel cannot make progress stalls every frame behind it. A W frame is
therefore always sent right after its AW frame.

Options
-------

Every field width is part of the wire format, so the options mirroring
``nsl_amba.axi4_mm.config()`` must match the RTL ``mm_config_c``:

``address_width`` (32), ``data_bus_width`` (bits, 32), ``id_width``
(0), ``user_width`` (0), ``max_length`` (1, power of two),
``size``, ``burst``, ``cache``, ``lock``, ``qos``, ``region``
(booleans, false).

Host behaviour:

``id``
    AXI ID of every transaction (default 0).
``prot``
    AxPROT value (default 0).
``max_burst``
    Longest burst issued, in beats (default ``max_length``).
``window``
    Transactions in flight before waiting for a response (default 8).
``timeout``
    Seconds to wait for each response (default 1.0).

Transactions
------------

A blob is split into INCR bursts of full-width beats, starting on a bus
word boundary, never longer than ``max_burst`` beats and never crossing
a 4 KiB boundary. Unaligned write heads and tails are masked with
strobes; unaligned reads are widened to whole words and trimmed.

All transactions use the same ID, so the slave completes writes in
issue order and reads in issue order, and responses are matched in
FIFO order per channel. AXI does not order reads against writes: when a
batch switches between reading and writing, the component waits for
every outstanding response first.

A SLVERR or DECERR response fails the blob the burst belongs to with
:class:`AxiResponseError`; other blobs of the batch proceed. A missing
response fails the rest of the batch with :class:`TimeoutError`, a
transport error (the simulator is not running: ICMP port unreachable)
with that error. When
``id_width`` is non-zero, the transaction ID then moves to the next
value so that late responses are recognized and dropped; with no ID
bits, late responses cannot be told apart from new ones.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from ...engine import BackgroundLowering, Batcher
from ...lifecycle import cancel_shutdown, on_shutdown
from ...node import Node
from ...protocol import datagram, memory
from ...util.pretty import bool_parse


class AxiResponseError(Exception):
    """A burst was answered with SLVERR or DECERR."""

    NAMES = ("OKAY", "EXOKAY", "SLVERR", "DECERR")

    def __init__(self, kind: str, addr: int, resp: int):
        super().__init__(
            f"AXI {kind} burst at {addr:#x} answered {self.NAMES[resp]}")
        self.kind = kind
        self.addr = addr
        self.resp = resp


class AxiProtocolError(Exception):
    """A response frame does not match the transaction it answers."""


@dataclass(frozen=True, slots=True)
class Burst:
    """One AXI INCR burst covering part of a blob.

    ``addr`` is bus-word aligned; ``offset`` is ``addr`` relative to the
    blob start (negative when the blob starts mid-word)."""

    addr: int
    beats: int
    offset: int


class Axi4MmStreamCodec:
    """Beat layouts of ``axi4_mm_on_stream`` for one bus configuration."""

    CH_B = 0
    CH_AW = 1
    CH_AR = 2
    CH_R = 3
    CH_W = 4

    RESP_OKAY = 0
    RESP_EXOKAY = 1
    RESP_SLVERR = 2
    RESP_DECERR = 3

    BURST_INCR = 1
    BOUNDARY = 4096

    def __init__(self, *, address_width: int = 32, data_bus_width: int = 32,
                 id_width: int = 0, user_width: int = 0, max_length: int = 1,
                 size: bool = False, burst: bool = False, cache: bool = False,
                 lock: bool = False, qos: bool = False, region: bool = False):
        if data_bus_width < 8 or data_bus_width % 8 \
           or (data_bus_width // 8) & (data_bus_width // 8 - 1):
            raise ValueError(
                f"data_bus_width must be 8 times a power of two, "
                f"got {data_bus_width}")
        if max_length < 1 or max_length & (max_length - 1) \
           or max_length > 256:
            raise ValueError(
                f"max_length must be a power of two up to 256, "
                f"got {max_length}")
        if not 1 <= address_width <= 64:
            raise ValueError(f"address_width out of range: {address_width}")

        self.address_width = address_width
        self.data_bytes = data_bus_width // 8
        self.size_l2 = self.data_bytes.bit_length() - 1
        self.id_width = id_width
        self.user_width = user_width
        self.len_width = max_length.bit_length() - 1
        self.has_size = size
        self.has_burst = burst and self.len_width != 0
        self.has_cache = cache
        self.has_lock = lock
        self.has_qos = qos
        self.has_region = region

        self.address_bits = (address_width + 3
                             + (3 if size else 0)
                             + (2 if self.has_burst else 0)
                             + (4 if cache else 0)
                             + self.len_width
                             + (1 if lock else 0)
                             + id_width
                             + (4 if qos else 0)
                             + (4 if region else 0)
                             + user_width)
        self.address_bytes = (self.address_bits + 7) // 8
        self.w_bytes = (9 * self.data_bytes + user_width + 7) // 8
        self.b_bytes = (2 + id_width + user_width + 7) // 8
        self.r_bytes = (8 * self.data_bytes + 2 + id_width + user_width
                        + 7) // 8

    @property
    def max_length(self) -> int:
        return 1 << self.len_width

    # -- host to device ------------------------------------------------

    def address(self, channel: int, addr: int, beats: int, axi_id: int,
                prot: int) -> bytes:
        """AW or AR frame for an INCR burst of ``beats`` full-width beats."""
        fields = [(addr, self.address_width), (prot, 3)]
        if self.has_size:
            fields.append((self.size_l2, 3))
        if self.has_burst:
            fields.append((self.BURST_INCR, 2))
        if self.has_cache:
            fields.append((0, 4))
        fields.append((beats - 1, self.len_width))
        if self.has_lock:
            fields.append((0, 1))
        fields.append((axi_id, self.id_width))
        if self.has_qos:
            fields.append((0, 4))
        if self.has_region:
            fields.append((0, 4))
        fields.append((0, self.user_width))
        return bytes([channel]) + self.pack(fields, self.address_bytes)

    def write_data(self, data: bytes, strobes: list[int]) -> bytes:
        """W frame. ``data`` holds every lane of every beat; ``strobes``
        has one lane mask per beat."""
        n = self.data_bytes
        out = bytearray([self.CH_W])
        for beat, strb in enumerate(strobes):
            lanes = int.from_bytes(data[beat * n:(beat + 1) * n], "little")
            out += self.pack([(lanes, 8 * n), (strb, n),
                              (0, self.user_width)], self.w_bytes)
        return bytes(out)

    # -- device to host ------------------------------------------------

    def write_response(self, payload: bytes) -> tuple[int, int]:
        """``(resp, id)`` of a B frame payload (channel byte removed)."""
        if len(payload) != self.b_bytes:
            raise AxiProtocolError(
                f"B frame of {len(payload)} bytes, expected {self.b_bytes}")
        v = int.from_bytes(payload, "little")
        return v & 3, (v >> 2) & ((1 << self.id_width) - 1)

    def read_data(self, payload: bytes) -> list[tuple[bytes, int, int]]:
        """``(data, resp, id)`` per beat of an R frame payload."""
        if not payload or len(payload) % self.r_bytes:
            raise AxiProtocolError(
                f"R frame of {len(payload)} bytes is not a whole number "
                f"of {self.r_bytes}-byte beats")
        n = self.data_bytes
        beats = []
        for off in range(0, len(payload), self.r_bytes):
            v = int.from_bytes(payload[off:off + self.r_bytes], "little")
            data = (v & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
            v >>= 8 * n
            beats.append((data, v & 3,
                          (v >> 2) & ((1 << self.id_width) - 1)))
        return beats

    # -- burst planning ------------------------------------------------

    def bursts(self, addr: int, size: int, max_burst: int) -> list[Burst]:
        """Cover ``[addr, addr + size)`` with aligned INCR bursts of at
        most ``max_burst`` beats that never cross a 4 KiB boundary."""
        n = self.data_bytes
        end = addr + size
        cursor = addr - addr % n
        out = []
        while cursor < end:
            boundary = (cursor // self.BOUNDARY + 1) * self.BOUNDARY
            stop = min(end, boundary, cursor + max_burst * n)
            beats = (stop - cursor + n - 1) // n
            out.append(Burst(cursor, beats, cursor - addr))
            cursor += beats * n
        return out

    def write_lanes(self, burst: Burst, data: bytes) -> tuple[bytes, list[int]]:
        """Lane bytes and per-beat strobes of ``burst`` for a blob write of
        ``data``: lanes outside the blob are zero and disabled."""
        n = self.data_bytes
        lanes = bytearray(burst.beats * n)
        strobes = []
        for beat in range(burst.beats):
            strb = 0
            for lane in range(n):
                pos = burst.offset + beat * n + lane
                if 0 <= pos < len(data):
                    lanes[beat * n + lane] = data[pos]
                    strb |= 1 << lane
            strobes.append(strb)
        return bytes(lanes), strobes

    @staticmethod
    def pack(fields: list[tuple[int, int]], byte_count: int) -> bytes:
        v = 0
        shift = 0
        for value, width in fields:
            if value < 0 or value >> width:
                raise ValueError(f"{value:#x} does not fit in {width} bits")
            v |= value << shift
            shift += width
        return v.to_bytes(byte_count, "little")


class Transaction:
    """One burst in flight: what the response must look like and the
    future it resolves."""

    def __init__(self, kind: str, burst: Burst, future: asyncio.Future):
        self.kind = kind
        self.burst = burst
        self.future = future


@datagram.Datagram.db.register("nsl_axi4_mm")
class Axi4MmOnStream(memory.RegisterFromBulk, BackgroundLowering, Batcher,
                     Node):
    """AXI4 master driving ``axi4_mm_on_stream`` through a datagram."""

    ops = memory.Interface.BULK_OPS

    CH_B = Axi4MmStreamCodec.CH_B
    CH_R = Axi4MmStreamCodec.CH_R

    BUS_OPTIONS = {
        "address_width": int, "data_bus_width": int, "id_width": int,
        "user_width": int, "max_length": int,
        "size": bool, "burst": bool, "cache": bool, "lock": bool,
        "qos": bool, "region": bool,
    }

    def __init__(self, transport: datagram.Datagram,
                 name: str = "nsl_axi4_mm"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.transport = transport
        self.codec = None
        self.__bus = {}
        self.__id = 0
        self.__prot = 0
        self.__max_burst = None
        self.__window = 8
        self.__timeout = 1.0
        self.__pending = {self.CH_B: deque(), self.CH_R: deque()}
        self.__rx_task = None

    def option_set(self, key, value):
        kind = self.BUS_OPTIONS.get(key)
        if kind is bool:
            self.__bus[key] = bool_parse(value)
        elif kind is int:
            self.__bus[key] = int(value, 0)
        elif key == "id":
            self.__id = int(value, 0)
        elif key == "prot":
            self.__prot = int(value, 0)
        elif key == "max_burst":
            self.__max_burst = int(value, 0)
        elif key == "window":
            self.__window = int(value, 0)
        elif key == "timeout":
            self.__timeout = float(value)

    async def start(self):
        self.codec = Axi4MmStreamCodec(**self.__bus)
        if self.__max_burst is None:
            self.__max_burst = self.codec.max_length
        if not 1 <= self.__max_burst <= self.codec.max_length:
            raise ValueError(
                f"max_burst must be 1..{self.codec.max_length}, "
                f"got {self.__max_burst}")
        if not 0 <= self.__id < 1 << self.codec.id_width:
            raise ValueError(
                f"id {self.__id} does not fit in {self.codec.id_width} bits")
        if not 0 <= self.__prot < 8:
            raise ValueError(f"prot must be 0..7, got {self.__prot}")
        if self.__window < 1:
            raise ValueError(f"window must be positive, got {self.__window}")
        if self.__timeout <= 0:
            raise ValueError(
                f"timeout must be positive, got {self.__timeout}")
        self.metadata["data_bus_width"] = self.codec.data_bytes * 8
        self.metadata["address_width"] = self.codec.address_width
        self.metadata["max_burst"] = self.__max_burst
        self.__rx_task = asyncio.create_task(self.__receive())
        on_shutdown(self.stop)

    async def stop(self):
        cancel_shutdown(self.stop)
        task = self.__rx_task
        self.__rx_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.__fail_pending(ConnectionError(f"{self.name} stopped"))

    # -- lowering ------------------------------------------------------

    async def flush_ops(self, batch):
        self.dispatch(batch)

    async def run_ops(self, batch):
        if self.__rx_task is None:
            raise ConnectionError(f"{self.name} is not started")
        loop = asyncio.get_running_loop()
        in_flight: deque[Transaction] = deque()
        in_flight_kind = None

        for op, future in batch:
            if isinstance(op, memory.ReadBlob):
                kind, size = "read", op.size
            elif isinstance(op, memory.WriteBlob):
                kind, size = "write", len(op.data)
            else:
                raise TypeError(f"Unsupported operation: {op!r}")

            if size == 0:
                if future is not None and not future.done():
                    future.set_result(b"" if kind == "read" else None)
                continue
            if op.addr < 0 or op.addr + size > 1 << self.codec.address_width:
                if future is not None and not future.done():
                    future.set_exception(ValueError(
                        f"[{op.addr:#x}, {op.addr + size:#x}) is outside the "
                        f"{self.codec.address_width}-bit address space"))
                continue

            # Every burst is attached before the first one is issued, so
            # the blob cannot resolve from a prefix of its bursts.
            txns = [Transaction(kind, burst, loop.create_future())
                    for burst in self.codec.bursts(op.addr, size,
                                                   self.__max_burst)]
            pending = None
            if future is not None:
                pending = memory.PendingBlob(future, size,
                                             is_read=kind == "read")
            for txn in txns:
                txn.future.add_done_callback(self.__consume)
                if pending is not None:
                    pending.attach(txn.burst.offset,
                                   txn.burst.beats * self.codec.data_bytes,
                                   txn.future)

            for txn in txns:
                if in_flight and in_flight_kind != kind:
                    while in_flight:
                        await self.__wait(in_flight.popleft())
                while len(in_flight) >= self.__window:
                    await self.__wait(in_flight.popleft())
                self.__issue(txn, op)
                in_flight.append(txn)
                in_flight_kind = kind

        while in_flight:
            await self.__wait(in_flight.popleft())

    def __issue(self, txn: Transaction, op) -> None:
        codec = self.codec
        burst = txn.burst
        if txn.kind == "write":
            self.__pending[self.CH_B].append(txn)
            lanes, strobes = codec.write_lanes(burst, op.data)
            frames = [
                codec.address(codec.CH_AW, burst.addr, burst.beats,
                              self.__id, self.__prot),
                codec.write_data(lanes, strobes),
            ]
        else:
            self.__pending[self.CH_R].append(txn)
            frames = [codec.address(codec.CH_AR, burst.addr, burst.beats,
                                    self.__id, self.__prot)]
        for frame in frames:
            self.transport.send(frame).add_done_callback(
                lambda f, txn=txn: self.__on_sent(f, txn))

    @staticmethod
    def __on_sent(f, txn):
        if f.cancelled():
            return
        exc = f.exception()
        if exc is not None and not txn.future.done():
            txn.future.set_exception(exc)

    @staticmethod
    def __consume(f):
        if not f.cancelled():
            f.exception()

    async def __wait(self, txn: Transaction) -> None:
        if txn.future.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(txn.future),
                                   self.__timeout)
        except TimeoutError:
            exc = TimeoutError(
                f"{self.name}: no response to {txn.kind} burst at "
                f"{txn.burst.addr:#x} within {self.__timeout} s")
            self.__fail_pending(exc)
            if self.codec.id_width:
                self.__id = (self.__id + 1) % (1 << self.codec.id_width)
            raise exc
        except (AxiResponseError, AxiProtocolError):
            # The blob carries the error; the link is still usable.
            pass

    def __fail_pending(self, exc: BaseException) -> None:
        for queue in self.__pending.values():
            while queue:
                txn = queue.popleft()
                if not txn.future.done():
                    txn.future.set_exception(exc)

    # -- responses -----------------------------------------------------

    async def __receive(self):
        while True:
            try:
                data, _ctx = await self.transport.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- fail waiters, go on
                self.logger.warning("receive failed: %s", exc)
                self.__fail_pending(exc)
                continue
            self.__on_frame(bytes(data))

    def __on_frame(self, frame: bytes) -> None:
        if not frame:
            self.logger.warning("empty frame dropped")
            return
        channel, payload = frame[0], frame[1:]
        queue = self.__pending.get(channel)
        if queue is None:
            self.logger.warning("frame on channel %d dropped (%d bytes)",
                                channel, len(payload))
            return
        if not queue:
            self.logger.warning(
                "unexpected %s frame dropped (%d bytes)",
                "B" if channel == self.CH_B else "R", len(payload))
            return

        try:
            if channel == self.CH_B:
                resp, axi_id = self.codec.write_response(payload)
                beats = None
            else:
                beats = self.codec.read_data(payload)
                resp = max(r for _, r, _ in beats)
                axi_id = beats[0][2]
        except AxiProtocolError as exc:
            txn = queue.popleft()
            if not txn.future.done():
                txn.future.set_exception(exc)
            return

        if axi_id != self.__id:
            self.logger.warning("response for ID %d dropped, expecting %d",
                                axi_id, self.__id)
            return

        txn = queue.popleft()
        if txn.future.done():
            return
        if resp >= self.codec.RESP_SLVERR:
            txn.future.set_exception(
                AxiResponseError(txn.kind, txn.burst.addr, resp))
        elif beats is None:
            txn.future.set_result(None)
        elif len(beats) != txn.burst.beats:
            txn.future.set_exception(AxiProtocolError(
                f"read burst at {txn.burst.addr:#x}: {len(beats)} beats "
                f"returned, {txn.burst.beats} expected"))
        else:
            txn.future.set_result(b"".join(d for d, _, _ in beats))
