"""NSL SWD transactor batch codec.

Encodes :mod:`acrobe.protocol.swd` operations into NSL-SWD command
byte streams and decodes the corresponding response byte streams
back into op results. No transport; no Batcher. The adapter-side
``SwdInterface`` subclass owns the transport and wires the codec to
it.

Command-stream encoding follows the iTap firmware (which matches
crobe's encoder modulo the divisor width — see ``divisor_width`` on
the constructor).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ....protocol import swd


class SwdTransactor:
    """Batch codec for the NSL SWD transactor command stream."""

    # Command bit layout (matches firmware constants):
    #   00xx_xxxx  RUN          count[5:0] (count+1 idle cycles)
    #   01xx_xxxx  RUN_DIO      count[5:0] (count+1 cycles with SWDIO=1)
    #   10xx_xxxx  RW           ap[5], read[4], reg[3:0]
    #                           read = 1 → response: ack + 4 data bytes
    #                           read = 0 → host appends 4 bytes; resp: ack
    #   1101_00xx  TURNAROUND   cycles[1:0] (cycles+1)
    #   1101_100x  SYS_RESET    assert[0]
    #   111x_xxxx  BITBANG      count[4:0] bits (count+1); host appends 4 bytes
    #   1100_0000  ABORT
    #   1100_0001  DIVISOR      host appends 1 or 2 bytes (see divisor_width)
    CMD_RUN          = 0x00
    CMD_RUN_DIO      = 0x40
    CMD_RW           = 0x80
    CMD_RW_R         = 0x10
    CMD_RW_AP        = 0x20
    CMD_TURNAROUND   = 0xd0
    CMD_SYS_RESET    = 0xd8
    CMD_BITBANG      = 0xe0
    CMD_ABORT        = 0xc0
    CMD_DIVISOR      = 0xc1

    # Response byte layout for RW transactions:
    #   bits[2:0]  ACK (1=OK, 2=WAIT, 4=FAULT)
    #   bit[3]     parity error (reads only)
    RSP_ACK_MASK     = 0x07
    RSP_PAR_ERROR    = 0x08

    # Encoder state attached to each Read/Write op for decode-time
    # response gathering. Stored in the `state` returned by encode().
    @dataclass(frozen=True, slots=True)
    class __Resolve:
        op_idx: int               # index into the batch
        rsp_offset: int           # offset in response bytes
        is_read: bool             # if True, 4 data bytes follow ack
        # TARGETSEL writes are spec'd to never be ACKed by any DP, so
        # the firmware-reported ACK byte for them is meaningless. When
        # set, decode resolves the op's future with None regardless of
        # the ACK value (no exception, no result).
        ignore_ack: bool = False

    def __init__(self, base_freq: float, *, divisor_width: int = 2,
                 max_chunk: int = 1024):
        if divisor_width not in (1, 2):
            raise ValueError(
                f"divisor_width must be 1 or 2, got {divisor_width!r}")

        self.base_freq = float(base_freq)
        self.divisor_width = divisor_width
        self.__max_divisor = (1 << (8 * divisor_width)) - 1
        self.max_chunk = max_chunk

        # Default to ~1 MHz half-cycle. Same default as crobe.
        d = max(0, int(self.base_freq / 1e6) - 1)
        self.__divisor = min(d, self.__max_divisor)
        self.__rate_dirty = True

        self.__turnaround_cycles = 1
        self.__turnaround_dirty = True

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def freq_update(self, freq: float | None) -> float:
        """Recompute divisor for `freq`. Returns the actual achievable
        SWCLK frequency."""
        if not freq:
            return self.base_freq / ((self.__divisor + 1) * 2)
        d = math.ceil(self.base_freq / float(freq) / 2) - 1
        d = max(0, min(d, self.__max_divisor))
        if d != self.__divisor:
            self.__divisor = d
            self.__rate_dirty = True
        return self.base_freq / ((self.__divisor + 1) * 2)

    @property
    def turnaround_cycles(self) -> int:
        return self.__turnaround_cycles

    @turnaround_cycles.setter
    def turnaround_cycles(self, n: int) -> None:
        n = int(n)
        if not 1 <= n <= 4:
            raise ValueError(f"turnaround_cycles must be 1..4, got {n}")
        if n != self.__turnaround_cycles:
            self.__turnaround_cycles = n
            self.__turnaround_dirty = True

    def context_force_refresh(self) -> None:
        """Force the next encode() to re-issue divisor + turnaround.
        Use after a target reset or any out-of-band wire-mode change."""
        self.__rate_dirty = True
        self.__turnaround_dirty = True

    # ------------------------------------------------------------------
    # Batch codec
    # ------------------------------------------------------------------

    def encode(self, batch) -> tuple[bytes, int, list]:
        """Encode `batch` (a list of (op, future) tuples) into a
        command byte stream. Returns ``(cmd_bytes, response_size,
        gather_state)``.

        Calls to :meth:`encode` MUST be paired with a call to
        :meth:`decode` carrying the response bytes for the returned
        size, so the dirty-flag bookkeeping stays consistent."""

        cmd = bytearray()
        rsp_size = 0
        gather: list = []

        if self.__turnaround_dirty:
            cmd.append(self.CMD_TURNAROUND | (self.__turnaround_cycles - 1))
            rsp_size += 1
            self.__turnaround_dirty = False

        if self.__rate_dirty:
            cmd.append(self.CMD_DIVISOR)
            cmd.extend(self.__divisor.to_bytes(self.divisor_width, "little"))
            rsp_size += 1
            self.__rate_dirty = False

        for op_idx, (op, _future) in enumerate(batch):
            rsp_size = self.__encode_one(op, op_idx, cmd, rsp_size, gather)

        return bytes(cmd), rsp_size, gather

    def __encode_one(self, op, op_idx, cmd, rsp_size, gather):
        if isinstance(op, swd.Read):
            # Insert a few idle cycles before the transaction. Crobe
            # uses CMD_RUN | 2 (3 cycles); keep the same default.
            cmd.append(self.CMD_RUN | 2)
            rsp_size += 1
            ap = bool(op.ap)
            reg = (op.addr & 0xC) >> 2  # only A[3:2] used on wire
            byte = self.CMD_RW | self.CMD_RW_R
            if ap:
                byte |= self.CMD_RW_AP
            byte |= reg & 0xf
            cmd.append(byte)
            gather.append(self.__Resolve(op_idx, rsp_size, True))
            rsp_size += 5  # ack + 4 data
            if ap:
                # Insert trailing idle cycles for AP read pipelining.
                # Crobe uses CMD_RUN | 10 (11 cycles).
                cmd.append(self.CMD_RUN | 10)
                rsp_size += 1
            return rsp_size

        if isinstance(op, swd.Write):
            cmd.append(self.CMD_RUN | 2)
            rsp_size += 1
            ap = bool(op.ap)
            reg = (op.addr & 0xC) >> 2
            byte = self.CMD_RW
            if ap:
                byte |= self.CMD_RW_AP
            byte |= reg & 0xf
            cmd.append(byte)
            cmd.extend(int(op.data & 0xFFFFFFFF).to_bytes(4, "little"))
            gather.append(self.__Resolve(op_idx, rsp_size, False))
            rsp_size += 1  # ack only
            if ap:
                cmd.append(self.CMD_RUN | 10)
                rsp_size += 1
            return rsp_size

        if isinstance(op, swd.Run):
            count = max(0, int(op.cycles))
            while count > 0:
                c = min(count, 64)
                cmd.append(self.CMD_RUN | (c - 1))
                rsp_size += 1
                count -= c
            return rsp_size

        if isinstance(op, swd.Wakeup):
            count = max(0, int(op.cycles))
            while count > 0:
                c = min(count, 64)
                cmd.append(self.CMD_RUN_DIO | (c - 1))
                rsp_size += 1
                count -= c
            return rsp_size

        if isinstance(op, swd.LineReset):
            # ≥50 cycles SWDIO=1, then ≥2 idle.
            rsp_size = self.__encode_one(swd.Wakeup(60), op_idx, cmd,
                                        rsp_size, gather)
            rsp_size = self.__encode_one(swd.Run(8), op_idx, cmd,
                                        rsp_size, gather)
            return rsp_size

        if isinstance(op, swd.JtagToSwd):
            # 50+ SWDIO=1, switch sequence (16 wire bits), 50+ SWDIO=1,
            # 16 idle. The NSL transactor's BITBANG reads 4 LE bytes
            # into a u32 and shifts them out LSB-first, so wire-bit 0
            # is bit 0 of the u32. ARM IHI0031 specifies the switch
            # sequence as 0x79E7 sent MSB-first; LSB-first wire
            # ordering of the same sequence is 0xE79E, packed LE as
            # bytes [0x9E, 0xE7].
            rsp_size = self.__encode_one(swd.Wakeup(60), op_idx, cmd,
                                        rsp_size, gather)
            cmd.append(self.CMD_BITBANG | (16 - 1))
            cmd.extend(b"\x9e\xe7\x00\x00")
            rsp_size += 1
            rsp_size = self.__encode_one(swd.Wakeup(60), op_idx, cmd,
                                        rsp_size, gather)
            rsp_size = self.__encode_one(swd.Run(16), op_idx, cmd,
                                        rsp_size, gather)
            return rsp_size

        if isinstance(op, swd.SwdToDormant):
            # 50+ SWDIO=1, then the 16-bit selection alert 0xE3BC
            # transmitted LSB-first. BITBANG shifts the u32 LSB-first
            # so the value is just 0xE3BC packed LE.
            rsp_size = self.__encode_one(swd.Wakeup(60), op_idx, cmd,
                                        rsp_size, gather)
            cmd.append(self.CMD_BITBANG | (16 - 1))
            cmd.extend((0xE3BC).to_bytes(4, "little"))
            rsp_size += 1
            return rsp_size

        if isinstance(op, swd.DormantToSwd):
            # 8+ SWDIO=1, 128-bit selection alert (bits[127:0] of
            # 0x19BC0EA2_E3DDAFE9_86852D95_6209F392, transmitted
            # LSB-first), 4 SWDIO=0, 8-bit SWD activation code 0x1A.
            rsp_size = self.__encode_one(swd.Wakeup(60), op_idx, cmd,
                                        rsp_size, gather)
            for u32 in (0x6209F392, 0x86852D95, 0xE3DDAFE9, 0x19BC0EA2):
                cmd.append(self.CMD_BITBANG | (32 - 1))
                cmd.extend(int(u32).to_bytes(4, "little"))
                rsp_size += 1
            rsp_size = self.__encode_one(swd.Run(4), op_idx, cmd,
                                        rsp_size, gather)
            cmd.append(self.CMD_BITBANG | (8 - 1))
            cmd.extend((0x1A).to_bytes(4, "little"))
            rsp_size += 1
            return rsp_size

        if isinstance(op, swd.TargetSelWrite):
            # Same wire shape as a DP Write to register 0x0c, except
            # ADIv5/v6 guarantees no DP responds with OK on the ACK
            # phase. We still issue a CMD_RW (write to DP A[3:2]=11)
            # so the firmware drives data + parity normally, and tag
            # the gather entry to ignore whatever ACK the wire reports.
            cmd.append(self.CMD_RUN | 2)
            rsp_size += 1
            reg = (0x0c & 0xc) >> 2          # = 0b11
            cmd.append(self.CMD_RW | (reg & 0xf))   # write, DP, A[3:2]=11
            cmd.extend(int(op.target & 0xFFFFFFFF).to_bytes(4, "little"))
            gather.append(self.__Resolve(
                op_idx, rsp_size, is_read=False, ignore_ack=True))
            rsp_size += 1
            return rsp_size

        raise TypeError(f"SwdTransactor cannot encode {type(op).__name__}")

    def decode(self, batch, response: bytes, gather) -> None:
        """Resolve each future in `batch` using the response bytes
        returned by the wire."""

        # Track per-op decoded result/exception so we can resolve
        # futures in a single pass at the end.
        results: dict[int, Any] = {}
        exceptions: dict[int, BaseException] = {}

        for entry in gather:
            ack_byte = response[entry.rsp_offset]
            if entry.ignore_ack:
                # TargetSelWrite path — ACK is meaningless on the wire.
                results[entry.op_idx] = None
                continue

            ack_bits = ack_byte & self.RSP_ACK_MASK
            par_err = bool(ack_byte & self.RSP_PAR_ERROR)

            try:
                ack = swd.Ack(ack_bits)
            except ValueError:
                exceptions[entry.op_idx] = swd.SwdAccessFailure(
                    f"invalid SWD ACK 0b{ack_bits:03b} "
                    f"(raw byte 0x{ack_byte:02x})")
                continue

            if ack == swd.Ack.WAIT:
                exceptions[entry.op_idx] = swd.SwdWait(
                    "WAIT (target not ready)")
                continue
            if ack == swd.Ack.FAULT:
                exceptions[entry.op_idx] = swd.SwdAccessFailure(
                    f"FAULT (raw byte 0x{ack_byte:02x})")
                continue

            # ACK = OK
            if entry.is_read:
                if par_err:
                    exceptions[entry.op_idx] = swd.SwdAccessFailure(
                        "data parity error")
                    continue
                data_bytes = response[entry.rsp_offset + 1:
                                      entry.rsp_offset + 5]
                results[entry.op_idx] = int.from_bytes(data_bytes, "little")
            else:
                results[entry.op_idx] = None

        # Resolve all futures. Ops not in gather (Run / Wakeup / …)
        # complete with None.
        for op_idx, (_op, future) in enumerate(batch):
            if future is None or future.done():
                continue
            if op_idx in exceptions:
                future.set_exception(exceptions[op_idx])
            elif op_idx in results:
                future.set_result(results[op_idx])
            else:
                future.set_result(None)

from ....engine import Batcher
from ....node import Node
from ....protocol import datagram, swd
from ....util.pretty import sci_parse

@datagram.Datagram.db.register("nsl_swd")
class SwdInterface(swd.Interface):
    def __init__(self, datagram, name: str = "swd"):
        self.__transactor = None
        self.__fin = 10e6
        self.__fmax = None
        self.__divisor_width = 2
        super().__init__(name)
        self.__transport = datagram

    async def start(self):
        self.__transactor = SwdTransactor(self.__fin, divisor_width = self.__divisor_width)
        if self.__fmax:
            self.__transactor.freq_update(self.__fmax)
        await super().start()

    def option_set(self, key, value):
        if not self.__transactor:
            if key == "fin":
                self.__fin = sci_parse(value)
                return
            if key == "fmax":
                self.__fmax = sci_parse(value)
                return
            if key == "divisor_width":
                self.__divisor_width = int(value)
                return
        else:
            self.__transactor.option_set(key, value)

    async def flush_wire_ops(self, batch):
        if not batch:
            return
        cmd, _rsp_size, gather = self.__transactor.encode(batch)
        self.__decode_on_recv(self.__transport, self.__transactor,
                              batch, cmd, gather)

    def freq_update(self, freq):
        if self.__transactor is None:
            return 1e3
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
