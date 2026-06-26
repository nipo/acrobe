"""NSL bnoc HDLC framing.

Two layered datagram adapters that bring the HDLC line coding from
``nsl_line_coding.hdlc`` to acrobe:

* :class:`Hdlc` wraps a :class:`~acrobe.protocol.pipe.Pipe`, frames
  each datagram with HDLC flags and byte stuffing, and protects the
  payload with a 16-bit FCS. Bad frames are dropped (logged at
  ``WARNING``).
* :class:`HdlcHeader` wraps an arbitrary
  :class:`~acrobe.protocol.datagram.Datagram` (typically an
  :class:`Hdlc`) and inserts the standard 2-byte HDLC header on
  send, stripping it again on receive. The address / control bytes
  are surfaced as an :class:`HdlcAddr` context.

For interoperation with ``nsl_line_coding.hdlc.hdlc_framed_*``
(used e.g. by ``axis_uart_trx``), stack
``HdlcHeader(Hdlc(pipe))``: the constructor defaults match the
RTL's fixed header (``address = 0x00``, ``control = 0x13`` — the
Unnumbered Information frame with P/F set that ``control_u(pf =>
true, t => "00000")`` produces).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from ..protocol.datagram import Datagram, Send, Recv
from ..protocol.pipe import Pipe
from ..util.crc import Crc


@dataclass(frozen=True, slots=True)
class HdlcAddr:
    """The two-byte HDLC header carried as datagram context.

    ``address`` is the HDLC ``address`` field (``A`` byte) and
    ``control`` is the HDLC ``control`` field (``C`` byte); both
    are 8-bit unsigned.
    """

    address: int
    control: int


@Pipe.db.register("hdlc")
class Hdlc(Datagram):
    """Datagram over a `Pipe` backend with HDLC framing and FCS.

    Send path: prepend ``0x7E``, byte-stuff payload + FCS, append
    ``0x7E``, push to the underlying pipe.

    Receive path: stream bytes from the pipe through a
    state-machine unframer. A flag byte (``0x7E``) ends the current
    frame; when the unescaped frame is at least 2 bytes long and
    its trailing 2-byte FCS matches, the payload (sans FCS) is
    queued for the next pending :class:`Recv`. Frames that fail the
    FCS or are too short are dropped with a WARNING-level log
    entry; this matches the NSL ``hdlc_framed_unframer`` behaviour
    (broken frames are simply not forwarded).
    """

    FLAG = 0x7E
    ESCAPE = 0x7D
    ESCAPE_MANGLE = 0x20

    # Bytes the NSL ``is_escaped`` function escapes. Includes the
    # flag and the escape itself, plus ETX and the two XON/XOFF
    # pairs (low and high-bit variants) so the encoded stream is
    # safe to carry over a UART with software flow control.
    ESCAPED_BYTES = frozenset({0x7D, 0x7E, 0x03, 0x11, 0x13, 0x91, 0x93})

    # Matches ``nsl_line_coding.hdlc.fcs_params_c``.
    fcs = Crc.from_name("hdlc")

    def __init__(self, pipe, name: str = "hdlc"):
        super().__init__(name)
        self.__pipe = pipe
        self.__frame: bytearray | None = None
        self.__escaped = False
        self.__frames: deque[bytes] = deque()

    @classmethod
    def escape(cls, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            if b in cls.ESCAPED_BYTES:
                out.append(cls.ESCAPE)
                out.append(b ^ cls.ESCAPE_MANGLE)
            else:
                out.append(b)
        return bytes(out)

    @classmethod
    def packetize(cls, payload: bytes) -> bytes:
        fcs = cls.fcs.calc_bytes(payload)
        return bytes([cls.FLAG]) + cls.escape(payload + fcs) + bytes([cls.FLAG])

    def __feed(self, data: bytes) -> None:
        for b in data:
            if b == self.FLAG:
                if self.__frame is not None and self.__frame:
                    self.__deliver(bytes(self.__frame))
                self.__frame = bytearray()
                self.__escaped = False
                continue
            if self.__frame is None:
                continue
            if self.__escaped:
                self.__frame.append(b ^ self.ESCAPE_MANGLE)
                self.__escaped = False
            elif b == self.ESCAPE:
                self.__escaped = True
            else:
                self.__frame.append(b)

    def __deliver(self, frame: bytes) -> None:
        if len(frame) < 2:
            self.logger.warning(
                "HDLC: short frame (%d bytes), dropping", len(frame))
            return
        if not self.fcs.is_valid(frame):
            self.logger.warning(
                "HDLC: bad FCS on %d-byte frame, dropping", len(frame))
            return
        self.__frames.append(frame[:-2])

    async def flush_ops(self, batch):
        sends = [(op, f) for op, f in batch if isinstance(op, Send)]
        recvs = [(op, f) for op, f in batch if isinstance(op, Recv)]

        if sends:
            blob = b"".join(self.packetize(op.data) for op, _ in sends)
            await self.__pipe.write(blob)
            for _, f in sends:
                if f is not None and not f.done():
                    f.set_result(None)

        for op, future in recvs:
            while not self.__frames:
                chunk = await self.__pipe.read(None)
                if not chunk:
                    raise EOFError("Hdlc: underlying pipe at EOF")
                self.__feed(chunk)
            frame = self.__frames.popleft()
            if future is None or future.done():
                continue
            future.set_result((frame, None))

    async def child_spawn(self, name):
        # ``addr<NN>`` stacks the 2-byte address/control header layer,
        # defaulting the address to <NN> (hex). Anything else defers to
        # the Datagram handler registry.
        if name.lower().startswith("addr"):
            try:
                address = int(name[4:], 16)
            except ValueError:
                pass
            else:
                return HdlcHeader(self, default_address=address)
        return await super().child_spawn(name)


class HdlcHeader(Datagram):
    """Datagram adapter adding the standard 2-byte HDLC header.

    Wraps another :class:`Datagram` (typically an :class:`Hdlc`).
    On send, prepends ``address`` and ``control`` bytes drawn from
    the :class:`HdlcAddr` carried in :attr:`Send.context`; when no
    context is supplied the constructor defaults are used. On
    receive, parses the first two bytes of the incoming payload as
    an :class:`HdlcAddr` and surfaces them as the recv context;
    the rest of the payload is returned as data.

    The default header bytes match the
    ``nsl_line_coding.hdlc.hdlc_framed_framer`` RTL: address ``0``,
    control ``0x13`` (UI frame, P/F set).
    """

    def __init__(self, lower: Datagram, *,
                 default_address: int = 0x00,
                 default_control: int = 0x13,
                 name: str = "hdlc-header"):
        super().__init__(name)
        self.__lower = lower
        self.__default = HdlcAddr(default_address, default_control)

    @property
    def default_header(self) -> HdlcAddr:
        return self.__default

    async def flush_ops(self, batch):
        sends = [(op, f) for op, f in batch if isinstance(op, Send)]
        recvs = [(op, f) for op, f in batch if isinstance(op, Recv)]

        if sends:
            send_futures = []
            for op, future in sends:
                hdr = op.context if op.context is not None else self.__default
                payload = bytes([hdr.address & 0xFF, hdr.control & 0xFF]) \
                    + op.data
                send_futures.append((self.__lower.send(payload), future))
            await asyncio.gather(*[wf for wf, _ in send_futures])
            for _, f in send_futures:
                if f is not None and not f.done():
                    f.set_result(None)

        if recvs:
            recv_futures = [
                (self.__lower.recv(), future) for _, future in recvs]
            for lower_fut, future in recv_futures:
                data, _ctx = await lower_fut
                if len(data) < 2:
                    if future is not None and not future.done():
                        future.set_exception(RuntimeError(
                            f"HdlcHeader: undersized frame "
                            f"({len(data)} bytes), expected at least 2 "
                            f"header bytes"))
                    continue
                hdr = HdlcAddr(data[0], data[1])
                if future is None or future.done():
                    continue
                future.set_result((bytes(data[2:]), hdr))
