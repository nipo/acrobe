"""NSL APB bus reached through ``apb_stream_bridge`` datagrams.

Host side of NSL ``nsl_amba.stream_apb.apb_stream_bridge``. The RTL
terminates a byte stream of commands and plays APB master; this
component turns memory accesses into its commands and decodes its
answers. The local memory-access surface is :mod:`acrobe.protocol.memory`
(bulk family native, register family served from it).

Path syntax::

    udp/<host>:<port>/nsl_apb(address_width=<a>,data_bus_width=<d>,burst_length_l2=<b>)

Wire format
-----------

One datagram carries one command, and is answered by one datagram.
Multi-byte fields are little-endian, of ``A = ceil(address_width / 8)``
address bytes and ``N = max(1, ceil(burst_length_l2 / 8))`` count
bytes; a word is ``D = data_bus_width / 8`` bytes, lane 0 first.

==========  ===============================================  ==========================
Command     Request                                          Answer
==========  ===============================================  ==========================
identify    ``ff``                                           identify bytes, status
read        ``80``, address (A), words - 1 (N)               ``words * D`` bytes, status
write       ``00``, address (A), ``words * D`` bytes         status
==========  ===============================================  ==========================

The status byte ends every answer: bit 0 set when a transfer of the
command answered PSLVERR, or when the command was malformed (unknown
opcode, frame ending inside the address, the count or a word). The
other bits are zero. The address is a byte address that auto-increments
by ``D`` per word; the RTL does not align it. Bytes after a read's count
are ignored, and a read of ``words - 1`` beyond ``2**burst_length_l2 - 1``
wraps silently in the RTL (it keeps ``burst_length_l2`` bits, at least
one), so the component never asks for more than ``2**burst_length_l2``
words. A write has no count: the frame's length makes it, and every
word is written with every byte lane enabled (``PSTRB`` all ones). A
transfer that fails does not stop the command: the remaining words are
still read (and returned) or written.

The bridge runs one command at a time and answers in command order;
nothing in an answer identifies its command.

Options
-------

Mirroring the RTL generics, required, as they define the wire:

``address_width``
    ``apb_config_c.address_width``, 1 to 32.
``data_bus_width``
    ``apb_config_c`` data bus width in bits: 8, 16 or 32.
``burst_length_l2``
    ``burst_length_l2_c``: a read moves up to ``2**burst_length_l2``
    words.

Host behaviour:

``max_write``
    Words per write command (default ``2**burst_length_l2``); a datagram
    carries ``1 + A + max_write * D`` bytes.
``window``
    Commands in flight before waiting for an answer (default 8).
``timeout``
    Seconds to wait for each answer (default 1.0).

Accesses
--------

Reads of any alignment and length are widened to the words covering
them, split into commands of at most ``2**burst_length_l2`` words, and
trimmed. Writes must cover whole, aligned words: the wire has no byte
strobes, and a read-modify-write of registers is not the bridge's to
decide. ``write8`` / ``write16`` on a 32-bit bus are refused the same
way, with :class:`ValueError`.

An answer with the error bit fails the blob its command belongs to with
:class:`ApbError`; other blobs of the batch proceed. A missing answer
fails the blob waiting for it and every later one of the batch with
:class:`TimeoutError`. A timed-out command keeps its place in the
answer order, so its late answer is recognized and dropped. A transport
error (the simulator is not running: ICMP port unreachable) fails every
command in flight with that error. A lost datagram, which the transport
does not report, desynchronizes answers from commands: stop and summon
the node again.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from ...engine import BackgroundLowering, Batcher
from ...lifecycle import cancel_shutdown, on_shutdown
from ...node import Node
from ...protocol import datagram, memory


class ApbError(Exception):
    """A command's status byte flags an error: PSLVERR on one of its
    transfers (or a command the bridge could not parse)."""

    def __init__(self, kind: str, addr: int, words: int, status: int):
        super().__init__(
            f"APB {kind} of {words} word(s) at {addr:#x} failed "
            f"(status {status:#04x})")
        self.kind = kind
        self.addr = addr
        self.words = words
        self.status = status


class ApbProtocolError(Exception):
    """An answer does not have the shape of the command it answers."""


@dataclass(frozen=True, slots=True)
class Identify:
    """Ask for the bridge's identify bytes."""


@dataclass(frozen=True, slots=True)
class Command:
    """One bridge command: ``addr`` word aligned, ``offset`` it relative
    to the blob start (negative when the blob starts mid-word)."""

    kind: str
    addr: int
    words: int
    offset: int


class ApbStreamCodec:
    """Command and answer layouts of ``apb_stream_bridge``."""

    OPCODE_WRITE = 0x00
    OPCODE_READ = 0x80
    OPCODE_IDENTIFY = 0xff
    STATUS_ERROR = 0x01

    def __init__(self, *, address_width: int, data_bus_width: int,
                 burst_length_l2: int):
        if not 1 <= address_width <= 32:
            raise ValueError(
                f"address_width must be 1..32, got {address_width}")
        if data_bus_width not in (8, 16, 32):
            raise ValueError(
                f"data_bus_width must be 8, 16 or 32, got {data_bus_width}")
        if not 0 <= burst_length_l2 <= 16:
            raise ValueError(
                f"burst_length_l2 must be 0..16, got {burst_length_l2}")
        self.address_width = address_width
        self.address_bytes = (address_width + 7) // 8
        self.data_bytes = data_bus_width // 8
        self.count_bytes = max(1, (burst_length_l2 + 7) // 8)
        self.max_read = 1 << burst_length_l2

    def identify(self) -> bytes:
        return bytes([self.OPCODE_IDENTIFY])

    def read(self, addr: int, words: int) -> bytes:
        if not 1 <= words <= self.max_read:
            raise ValueError(
                f"a read moves 1..{self.max_read} words, not {words}")
        return (bytes([self.OPCODE_READ])
                + addr.to_bytes(self.address_bytes, "little")
                + (words - 1).to_bytes(self.count_bytes, "little"))

    def write(self, addr: int, data: bytes) -> bytes:
        if not data or len(data) % self.data_bytes:
            raise ValueError(
                f"a write moves whole {self.data_bytes}-byte words, "
                f"not {len(data)} bytes")
        return (bytes([self.OPCODE_WRITE])
                + addr.to_bytes(self.address_bytes, "little")
                + bytes(data))

    @classmethod
    def answer(cls, frame: bytes) -> tuple[bytes, int]:
        """``(payload, status)`` of an answer."""
        if not frame:
            raise ApbProtocolError("empty answer")
        return frame[:-1], frame[-1]

    def reads(self, addr: int, size: int) -> list[Command]:
        """Cover ``[addr, addr + size)`` with read commands of whole
        words."""
        n = self.data_bytes
        end = addr + size
        cursor = addr - addr % n
        out = []
        while cursor < end:
            words = min(self.max_read, (end - cursor + n - 1) // n)
            out.append(Command("read", cursor, words, cursor - addr))
            cursor += words * n
        return out

    def writes(self, addr: int, size: int, max_words: int) -> list[Command]:
        """Split an aligned, whole-word write into commands."""
        n = self.data_bytes
        if addr % n or size % n:
            raise ValueError(
                f"the bridge writes whole {n}-byte words: "
                f"[{addr:#x}, {addr + size:#x}) is not")
        out = []
        for offset in range(0, size, max_words * n):
            words = min(max_words, (size - offset) // n)
            out.append(Command("write", addr + offset, words, offset))
        return out


class Pending:
    """A command in flight: what its answer must look like and the
    future it resolves."""

    def __init__(self, kind: str, addr: int, words: int,
                 future: asyncio.Future):
        self.kind = kind
        self.addr = addr
        self.words = words
        self.future = future


@datagram.Datagram.db.register("nsl_apb")
class ApbOnStream(memory.RegisterFromBulk, BackgroundLowering, Batcher,
                  Node):
    """APB master driving ``apb_stream_bridge`` through a datagram."""

    ops = memory.Interface.BULK_OPS | {Identify}

    REQUIRED = ("address_width", "data_bus_width", "burst_length_l2")

    def __init__(self, transport: datagram.Datagram,
                 name: str = "nsl_apb"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.transport = transport
        self.codec = None
        self.__bus = {}
        self.__max_write = None
        self.__window = 8
        self.__timeout = 1.0
        self.__pending: deque[Pending] = deque()
        self.__rx_task = None

    def option_set(self, key, value):
        if key in self.REQUIRED:
            self.__bus[key] = int(value, 0)
        elif key == "max_write":
            self.__max_write = int(value, 0)
        elif key == "window":
            self.__window = int(value, 0)
        elif key == "timeout":
            self.__timeout = float(value)

    async def start(self):
        missing = [k for k in self.REQUIRED if k not in self.__bus]
        if missing:
            raise ValueError(
                f"nsl_apb needs {', '.join(missing)}: they define the wire")
        self.codec = ApbStreamCodec(**self.__bus)
        if self.__max_write is None:
            self.__max_write = self.codec.max_read
        if self.__max_write < 1:
            raise ValueError(
                f"max_write must be positive, got {self.__max_write}")
        if self.__window < 1:
            raise ValueError(f"window must be positive, got {self.__window}")
        if self.__timeout <= 0:
            raise ValueError(
                f"timeout must be positive, got {self.__timeout}")
        self.metadata["address_width"] = self.codec.address_width
        self.metadata["data_bus_width"] = self.codec.data_bytes * 8
        self.metadata["max_read"] = self.codec.max_read
        self.metadata["max_write"] = self.__max_write
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

    def identify(self):
        """The bridge's identify bytes (its ``identify_c``)."""
        return self.post(Identify())

    # -- lowering ------------------------------------------------------

    async def flush_ops(self, batch):
        self.dispatch(batch)

    async def run_ops(self, batch):
        if self.__rx_task is None:
            raise ConnectionError(f"{self.name} is not started")
        loop = asyncio.get_running_loop()
        in_flight: deque[Pending] = deque()

        for op, future in batch:
            try:
                issues = self.__lower(op, future, loop)
            except (ValueError, TypeError) as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
                continue
            for pending, frame in issues:
                while len(in_flight) >= self.__window:
                    await self.__wait(in_flight.popleft())
                self.__issue(pending, frame)
                in_flight.append(pending)

        while in_flight:
            await self.__wait(in_flight.popleft())

    def __lower(self, op, future, loop) -> list[tuple[Pending, bytes]]:
        """Commands of one op, each with the future its answer resolves,
        chained to ``future``. Every command is built before the first
        is issued, so a refused op sends nothing."""
        codec = self.codec
        if isinstance(op, Identify):
            pending = Pending("identify", 0, 0, loop.create_future())
            pending.future.add_done_callback(self.__consume)
            if future is not None:
                pending.future.add_done_callback(
                    lambda f: self.__forward(f, future))
            return [(pending, codec.identify())]

        if isinstance(op, memory.ReadBlob):
            kind, size = "read", op.size
        elif isinstance(op, memory.WriteBlob):
            kind, size = "write", len(op.data)
        else:
            raise TypeError(f"Unsupported operation: {op!r}")

        if size == 0:
            if future is not None and not future.done():
                future.set_result(b"" if kind == "read" else None)
            return []
        if op.addr < 0 or op.addr + size > 1 << codec.address_width:
            raise ValueError(
                f"[{op.addr:#x}, {op.addr + size:#x}) is outside the "
                f"{codec.address_width}-bit address space")

        if kind == "read":
            commands = codec.reads(op.addr, size)
        else:
            commands = codec.writes(op.addr, size, self.__max_write)

        blob = None
        if future is not None:
            blob = memory.PendingBlob(future, size, is_read=kind == "read")
        out = []
        for cmd in commands:
            pending = Pending(kind, cmd.addr, cmd.words, loop.create_future())
            pending.future.add_done_callback(self.__consume)
            if blob is not None:
                blob.attach(cmd.offset, cmd.words * codec.data_bytes,
                            pending.future)
            if kind == "read":
                frame = codec.read(cmd.addr, cmd.words)
            else:
                lo = cmd.offset
                hi = lo + cmd.words * codec.data_bytes
                frame = codec.write(cmd.addr, op.data[lo:hi])
            out.append((pending, frame))
        return out

    @staticmethod
    def __forward(f, future):
        if future.done() or f.cancelled():
            return
        exc = f.exception()
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(f.result())

    @staticmethod
    def __consume(f):
        if not f.cancelled():
            f.exception()

    def __issue(self, pending: Pending, frame: bytes) -> None:
        self.__pending.append(pending)
        self.transport.send(frame).add_done_callback(
            lambda f: self.__on_sent(f, pending))

    def __on_sent(self, f, pending: Pending):
        if f.cancelled():
            return
        exc = f.exception()
        if exc is not None:
            self.__fail_pending(exc)

    async def __wait(self, pending: Pending) -> None:
        if pending.future.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(pending.future),
                                   self.__timeout)
        except TimeoutError:
            exc = TimeoutError(
                f"{self.name}: no answer to {pending.kind} at "
                f"{pending.addr:#x} within {self.__timeout} s")
            # Commands keep their place: late answers are dropped in
            # order.
            for p in self.__pending:
                if not p.future.done():
                    p.future.set_exception(exc)
            raise exc
        except (ApbError, ApbProtocolError):
            # The blob carries the error; the link is still usable.
            pass

    def __fail_pending(self, exc: BaseException) -> None:
        while self.__pending:
            pending = self.__pending.popleft()
            if not pending.future.done():
                pending.future.set_exception(exc)

    # -- answers -------------------------------------------------------

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
            self.__on_answer(bytes(data))

    def __on_answer(self, frame: bytes) -> None:
        if not self.__pending:
            self.logger.warning("unexpected answer dropped (%d bytes)",
                                len(frame))
            return
        pending = self.__pending.popleft()
        if pending.future.done():
            return
        try:
            payload, status = self.codec.answer(frame)
            expected = pending.words * self.codec.data_bytes \
                if pending.kind == "read" else 0
            if pending.kind != "identify" and len(payload) != expected:
                raise ApbProtocolError(
                    f"{pending.kind} at {pending.addr:#x}: {len(payload)} "
                    f"bytes answered, {expected} expected")
        except ApbProtocolError as exc:
            pending.future.set_exception(exc)
            return
        if status & ApbStreamCodec.STATUS_ERROR:
            pending.future.set_exception(
                ApbError(pending.kind, pending.addr, pending.words, status))
        elif pending.kind == "write":
            pending.future.set_result(None)
        else:
            pending.future.set_result(payload)
