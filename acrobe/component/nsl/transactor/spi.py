"""NSL SPI transactor batch codec.

Encodes :mod:`acrobe.protocol.spi` Cs/Shift operations into NSL-SPI
transactor command byte streams and decodes the response back into op
results. Pure codec: no transport, no Batcher. The adapter-side
``SpiInterface`` subclass owns the transport and wires the codec to
it.

The RTL widened its clock divisor from 5 to 7 bits by splitting the
single divisor command in two, so a block implements one of two
mutually incompatible encodings — see ``divisor_scheme`` on the
constructor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ....protocol.spi import Cs, Shift


class SpiTransactor:
    """Batch codec for the NSL SPI transactor command stream."""

    # Command bit layout (matches RTL nsl_spi_transactor):
    #   0xxx_xxxx  SELECT       cpol[4], cpha[3], slave_id[2:0] (7 = UNSELECT)
    #   01xx_xxxx  SHIFT_IN     count[5:0]+1 (read-only)
    #   10xx_xxxx  SHIFT_OUT    count[5:0]+1 (write-only); host appends mosi
    #   11xx_xxxx  SHIFT_INOUT  count[5:0]+1 (full duplex); host appends mosi
    #
    # "div5" scheme:
    #   001x_xxxx  DIV          div[4:0]
    #
    # "divh_divl" scheme, which carves the same opcode space up:
    #   0010_xxxx  DIVH         div[6:3], and clears div[2:0]
    #   0011_0xxx  DIVL         div[2:0]
    #   0011_1xxx  WIDTH        shift word width in bits, minus one
    CMD_SELECT       = 0x00
    UNSELECT_ID      = 0x07
    CMD_DIV          = 0x20
    CMD_DIVH         = 0x20
    CMD_DIVL         = 0x30
    CMD_WIDTH        = 0x38
    CMD_SHIFT_IN     = 0x40
    CMD_SHIFT_OUT    = 0x80
    CMD_SHIFT_INOUT  = 0xC0

    # The divisor register holds one SCK half-period, minus one host
    # clock cycle. Its width is what the two schemes disagree on.
    DIVISOR_MAX = {"div5": 0x1f, "divh_divl": 0x7f}

    MAX_CHUNK = 0x40  # 64 bytes per shift command

    @dataclass(frozen=True, slots=True)
    class __Gather:
        op_idx: int
        rsp_offset: int
        length: int

    def __init__(self, base_freq: float, *, divisor_scheme: str = "divh_divl",
                 max_chunk: int | None = None):
        """`divisor_scheme` names the divisor encoding the block was
        built with:

        * ``"divh_divl"`` — one DIVH plus one DIVL command carry a
          7-bit divisor. What NSL builds today.
        * ``"div5"`` — one DIV command carries a 5-bit divisor. What
          NSL built before the divisor was widened; a block of that
          vintage reads a DIVH command as a DIV one.

        The two are not detectable on the wire, and neither rejects
        the other's command stream: a ``div5`` divisor of 15 or less
        programs eight times itself on a ``divh_divl`` block and a
        larger one lands on that block's DIVL or WIDTH command, while
        a ``divh_divl`` pair read by a ``div5`` block leaves the
        divisor wherever its second byte happens to point.
        """
        if divisor_scheme not in self.DIVISOR_MAX:
            raise ValueError(
                f"divisor_scheme must be one of "
                f"{', '.join(sorted(self.DIVISOR_MAX))}, "
                f"got {divisor_scheme!r}")

        self.base_freq = float(base_freq)
        self.divisor_scheme = divisor_scheme
        self.__max_divisor = self.DIVISOR_MAX[divisor_scheme]
        if max_chunk is not None:
            self.max_chunk = int(max_chunk)
        else:
            self.max_chunk = self.MAX_CHUNK
        d = max(0, int(self.base_freq / 1e6) - 1)
        self.__divisor = min(d, self.__max_divisor)
        self.__rate_dirty = True

    @property
    def divisor(self) -> int:
        """Divisor register value the next encode() will program."""
        return self.__divisor

    def freq_update(self, freq: float | None) -> float:
        if not freq:
            return self.base_freq / ((self.__divisor + 1) * 2)
        d = int(math.ceil(self.base_freq / 2.0 / float(freq))) - 1
        d = max(0, min(d, self.__max_divisor))
        if d != self.__divisor:
            self.__divisor = d
            self.__rate_dirty = True
        return self.base_freq / ((self.__divisor + 1) * 2)

    def context_force_refresh(self) -> None:
        self.__rate_dirty = True

    def __divisor_cmd(self) -> bytes:
        if self.divisor_scheme == "div5":
            return bytes([self.CMD_DIV | self.__divisor])
        return bytes([self.CMD_DIVH | (self.__divisor >> 3),
                      self.CMD_DIVL | (self.__divisor & 0x7)])

    def encode(self, batch) -> tuple[bytes, int, list]:
        cmd = bytearray()
        rsp_size = 0
        gather: list = []
        mode = 0

        if self.__rate_dirty:
            divisor_cmd = self.__divisor_cmd()
            cmd.extend(divisor_cmd)
            rsp_size += len(divisor_cmd)
            self.__rate_dirty = False

        for op_idx, (op, _future) in enumerate(batch):
            if isinstance(op, Cs):
                if op.value is not None:
                    mode = op.mode
                    cpol = (mode >> 1) & 1
                    cpha = mode & 1
                    cmd.append(self.CMD_SELECT
                               | (cpol << 4) | (cpha << 3)
                               | (op.value & 0x7))
                else:
                    cpol = (mode >> 1) & 1
                    cpha = mode & 1
                    cmd.append(self.CMD_SELECT
                               | (cpol << 4) | (cpha << 3)
                               | self.UNSELECT_ID)
                rsp_size += 1
                continue

            if isinstance(op, Shift):
                mosi_bytes = bytes(op.mosi)
                if op.read_miso:
                    base_cmd = self.CMD_SHIFT_INOUT
                else:
                    base_cmd = self.CMD_SHIFT_OUT
                if not mosi_bytes:
                    continue
                for off in range(0, len(mosi_bytes), self.max_chunk):
                    chunk = mosi_bytes[off:off + self.max_chunk]
                    cmd.append(base_cmd | (len(chunk) - 1))
                    cmd.extend(chunk)
                    rsp_size += 1
                    if op.read_miso:
                        gather.append(self.__Gather(op_idx, rsp_size,
                                                    len(chunk)))
                        rsp_size += len(chunk)
                continue

            raise TypeError(f"SpiTransactor cannot encode {type(op).__name__}")

        return bytes(cmd), rsp_size, gather

    def decode(self, batch, response: bytes, gather) -> None:
        per_op: dict[int, bytearray] = {}
        for g in gather:
            per_op.setdefault(g.op_idx, bytearray()).extend(
                response[g.rsp_offset:g.rsp_offset + g.length])

        for op_idx, (op, future) in enumerate(batch):
            if isinstance(op, Shift) and op.read_miso:
                miso = bytes(per_op.get(op_idx, b""))
                op.miso = miso
                if future is not None and not future.done():
                    future.set_result(miso)
                continue

            if isinstance(op, Shift):
                op.miso = None
            if future is not None and not future.done():
                future.set_result(None)

from ....engine import Batcher
from ....node import Node
from ....protocol import datagram, spi
from ....util.pretty import sci_parse

@datagram.Datagram.db.register("nsl_spi")
class SpiInterface(spi.Interface):
    def __init__(self, datagram, name: str = "spi"):
        self.__transactor = None
        self.__fin = 10e6
        self.__cs_map = 1
        self.__divisor_scheme = "divh_divl"
        super().__init__(datagram, name)

    async def start(self):
        self.__transactor = SpiTransactor(
            self.__fin, divisor_scheme=self.__divisor_scheme)
        self.freq_reapply()
        for cs in range(4):
            if (1 << cs) & self.__cs_map:
                target = spi.Target(self, cs=cs, mode=0, name=f"cs{cs}")
                self.child_add(target)

    def option_set(self, key, value):
        if self.__transactor:
            return
        if key == "fin":
            self.__fin = sci_parse(value)
        elif key == "cs_map":
            self.__cs_map = int(value)
        elif key == "divisor_scheme":
            self.__divisor_scheme = value

    async def flush_ops(self, batch):
        cmd, _rsp_size, gather = self.__transactor.encode(batch)
        self.__decode_on_recv(self.parent, self.__transactor, batch, cmd, gather)

    def freq_update(self, freq):
        if self.__transactor is None:
            return 0.0
        return self.__transactor.freq_update(freq)

    @staticmethod
    def __decode_on_recv(endpoint, codec, batch, cmd, gather):
        """Non-blocking transactor flush: post the command and one recv,
        then decode the response into the batch's futures from the recv
        callback. Never awaits — see the Batcher flush_ops contract."""
        endpoint.send(cmd)
        rf = endpoint.recv()

        def done(f):
            exc = f.exception()
            if exc is None:
                try:
                    rsp, _ctx = f.result()
                    codec.decode(batch, rsp, gather)
                    return
                except BaseException as e:   # noqa: BLE001 — forward to futures
                    exc = e
            for _op, fut in batch:
                if fut is not None and not fut.done():
                    fut.set_exception(exc)

        rf.add_done_callback(done)
