"""NSL I2C transactor batch codec.

Encodes :class:`acrobe.protocol.i2c.Transaction` operations as
NSL-I2C transactor command bytes and decodes the response back into
the natural per-item result. Pure codec: no transport, no Batcher.

Each :class:`acrobe.protocol.i2c.Transfer` lowers to one
START/addr/(write)/(repeated-START/addr/read)/STOP wire transaction;
the final read byte of a Transfer uses ``READ_NACK`` so the slave
sees the proper end-of-burst signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ....protocol import i2c


class I2cTransactor:
    """Batch codec for the NSL I²C transactor command stream."""

    # Command bit layout (matches RTL nsl_i2c.transactor):
    #   000x_xxxx  DIV         divisor[4:0]
    #   001x_xxxx  STOP        STOP condition (no payload)
    #   001x_xxxx  START       START condition (CMD_STOP | x?). See below.
    #   01xx_xxxx  WRITE       count[5:0]+1; host appends count bytes
    #   10xx_xxxx  READ_NACK   count[5:0]+1; device returns count data bytes
    #                          (final byte gets NACK, then STOP-friendly)
    #   11xx_xxxx  READ_ACK    count[5:0]+1; device ACKs every byte
    CMD_DIV       = 0x00
    CMD_START     = 0x20
    CMD_STOP      = 0x21
    CMD_WRITE     = 0x40
    CMD_READ_NACK = 0x80
    CMD_READ_ACK  = 0xc0

    # 5-bit pre-divisor (firmware-side), gives the bus a ÷32 base divisor
    # before the 5-bit `div` field. Matches crobe.
    PRE_DIV = 32

    MAX_CHUNK = 0x40  # 64 bytes per WRITE / READ command

    @dataclass(frozen=True, slots=True)
    class __TransferGather:
        op_idx: int               # batch index of the parent Transaction
        item_idx: int             # index of this Transfer in the Transaction
        addr_w_rsp_offset: int    # status byte for the write-addr (0 if no write)
        addr_r_rsp_offset: int    # status byte for the read-addr (0 if no read)
        write_rsp_offset: int     # status byte slot for the write payload
        write_size: int           # byte count written
        read_rsp_offset: int      # first data byte for the read payload
        read_size: int            # byte count read
        addr: int

    def __init__(self, base_freq: float):
        self.base_freq = float(base_freq)
        # Default to ~100 kHz; firmware divisor is 5 bits, clamp to range.
        self.__divisor = self.__divisor_for(100e3)
        self.__rate_dirty = True

    def __divisor_for(self, freq: float) -> int:
        if not freq:
            return 0x1f
        d = math.ceil(self.base_freq / float(freq) / self.PRE_DIV / 2)
        return max(2, min(int(d), 0x1f))

    def freq_update(self, freq: float | None) -> float:
        d = self.__divisor_for(freq or 0)
        if d != self.__divisor:
            self.__divisor = d
            self.__rate_dirty = True
        return self.base_freq / self.__divisor / self.PRE_DIV / 2

    def context_force_refresh(self) -> None:
        self.__rate_dirty = True

    def encode(self, batch) -> tuple[bytes, int, list]:
        cmd = bytearray()
        rsp_size = 0
        gather: list = []

        if self.__rate_dirty:
            cmd.append(self.CMD_DIV | self.__divisor)
            rsp_size += 1
            self.__rate_dirty = False

        for op_idx, (op, _future) in enumerate(batch):
            if not isinstance(op, i2c.Transaction):
                raise TypeError(
                    f"I2cTransactor expects Transaction, got "
                    f"{type(op).__name__}")
            for item_idx, item in enumerate(op.items):
                if isinstance(item, i2c.WaitAck):
                    raise NotImplementedError(
                        "I2cTransactor: WaitAck not supported")
                if not isinstance(item, i2c.Transfer):
                    raise TypeError(
                        f"I2cTransactor cannot encode {type(item).__name__}")
                rsp_size = self.__encode_transfer(
                    item, op_idx, item_idx, cmd, rsp_size, gather)

        return bytes(cmd), rsp_size, gather

    def __encode_transfer(self, t, op_idx, item_idx,
                          cmd, rsp_size, gather):
        addr_w_off = 0
        addr_r_off = 0
        write_off = 0
        read_off = 0

        # START
        cmd.append(self.CMD_START)
        rsp_size += 1

        # Write phase (always at least the addressed byte if data_w is non-empty)
        if t.data_w:
            cmd.append(self.CMD_WRITE | 0)
            cmd.append((t.addr << 1) & 0xfe)  # write address
            addr_w_off = rsp_size + 1
            rsp_size += 2

            # Payload (chunks of MAX_CHUNK)
            data = t.data_w
            for off in range(0, len(data), self.MAX_CHUNK):
                chunk = data[off:off + self.MAX_CHUNK]
                cmd.append(self.CMD_WRITE | (len(chunk) - 1))
                cmd.extend(chunk)
                if off == 0:
                    write_off = rsp_size
                rsp_size += len(chunk)

        # Repeated START + read phase
        if t.size_r:
            cmd.append(self.CMD_START)
            rsp_size += 1
            cmd.append(self.CMD_WRITE | 0)
            cmd.append((t.addr << 1) | 0x01)  # read address
            addr_r_off = rsp_size + 1
            rsp_size += 2

            # Reads: every chunk but the last uses READ_ACK; last byte
            # of the last chunk uses READ_NACK.
            remaining = t.size_r
            read_off = rsp_size
            while remaining > 0:
                chunk = min(remaining, self.MAX_CHUNK)
                last_chunk = (remaining - chunk) == 0
                if last_chunk:
                    cmd.append(self.CMD_READ_NACK | (chunk - 1))
                else:
                    cmd.append(self.CMD_READ_ACK | (chunk - 1))
                rsp_size += chunk
                remaining -= chunk

        # STOP
        cmd.append(self.CMD_STOP)
        rsp_size += 1

        gather.append(self.__TransferGather(
            op_idx=op_idx,
            item_idx=item_idx,
            addr_w_rsp_offset=addr_w_off,
            addr_r_rsp_offset=addr_r_off,
            write_rsp_offset=write_off,
            write_size=len(t.data_w),
            read_rsp_offset=read_off,
            read_size=t.size_r,
            addr=t.addr,
        ))
        return rsp_size

    def decode(self, batch, response: bytes, gather) -> None:
        # Bucket Transfer results per Transaction (op_idx).
        per_op: dict[int, list] = {}
        per_op_exc: dict[int, BaseException] = {}

        for g in gather:
            results = per_op.setdefault(g.op_idx, [])
            if g.op_idx in per_op_exc:
                # Already failed earlier in this Transaction; skip.
                continue
            try:
                self.__decode_transfer(g, response, results)
            except BaseException as exc:
                per_op_exc[g.op_idx] = exc

        for op_idx, (_op, future) in enumerate(batch):
            if future is None or future.done():
                continue
            if op_idx in per_op_exc:
                future.set_exception(per_op_exc[op_idx])
                continue
            future.set_result(tuple(per_op.get(op_idx, [])))

    @staticmethod
    def __decode_transfer(g, response, results):
        # Address ACKs: status byte == 1 means ACK; 0 means NACK.
        if g.write_size and not response[g.addr_w_rsp_offset]:
            raise i2c.AddressNack(g.addr)
        if g.read_size and not response[g.addr_r_rsp_offset]:
            raise i2c.AddressNack(g.addr)

        # Write-payload bytes also produce per-byte status; any 0 means
        # the slave NACKed mid-burst. Last byte is allowed to NACK by
        # some slaves (they're saying "no more"); we still flag it.
        if g.write_size:
            slice_ = response[g.write_rsp_offset:
                              g.write_rsp_offset + g.write_size]
            if not all(slice_):
                raise i2c.DataNack(g.addr)

        # Read payload.
        if g.read_size:
            data = response[g.read_rsp_offset:
                            g.read_rsp_offset + g.read_size]
            results.append(bytes(data))
        else:
            results.append(None)
