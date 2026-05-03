"""J-Link SWD: a Dp variant that drives SWD packets directly via
``CMD_SWD_IO`` bit-bang.

Pragmatic shortcut while there's only one SWD backend: combines
the wire-protocol layer (slice 7's SwDp role) and the bit-bang
adapter into one class. When a second SWD adapter shows up
(CMSIS-DAP, FTDI MPSSE in SWD mode, …) we can refactor to
:class:`swd.Interface` + a generic SwDp(Dp).

Owns:

* Wire-mode entry (line reset → JTAG-to-SWD switch → line reset →
  idle), per ARM ADIv5/v6.
* SELECT cache (APSEL + APBANKSEL + DPBANKSEL) so consecutive AP
  accesses in the same bank don't trip extra DP-write packets.
* AP-read data pipelining: an AP read's ACK lands on its own
  packet, the data lands on the *next* packet (per spec). On
  end-of-batch we drain any trailing pending read with a DP RDBUFF
  read.
"""

from __future__ import annotations

import asyncio

from ...component.arm import dp as dpmod
from . import protocol


# ---- Packing helpers (LSB-first byte streams) --------------------

def _pack_bits(bits) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _unpack_bits(data: bytes, count: int):
    return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]


# ---- SWD packet helpers ------------------------------------------
#
# Packet layout (46 bits):
#   READ:   cmd[0..7]  TRN[8]  ACK[9..11]  data[12..43]  parity[44]  TRN[45]
#   WRITE:  cmd[0..7]  TRN[8]  ACK[9..11]  TRN[12]       data[13..44] parity[45]
#
# ACK encoding: 0b001 = OK, 0b010 = WAIT, 0b100 = FAULT (LSB-first).

_SWD_PACKET_BITS = 46
_ACK_OFFSET = 9
_READ_DATA_OFFSET = 12
_READ_PARITY_OFFSET = 44

_ACK_OK    = 0b001
_ACK_WAIT  = 0b010
_ACK_FAULT = 0b100


def _swd_cmd_byte(ap: bool, rnw: bool, addr: int) -> int:
    """8-bit SWD request packet header.

    Bit layout (LSB-first wire):
        0: start (1)
        1: APnDP
        2: RnW
        3: A[2]
        4: A[3]
        5: parity (even over bits 1..4)
        6: stop (0)
        7: park (1)
    """
    a = (addr >> 2) & 0x3
    parity = (int(ap) ^ (a & 1) ^ ((a >> 1) & 1) ^ int(rnw)) & 1
    return (0x81 | (0x4 if rnw else 0)
            | (int(ap) << 1)
            | (a << 3)
            | (parity << 5))


def _data_parity(data: int) -> int:
    """Even parity over a 32-bit data word."""
    x = data & 0xFFFFFFFF
    x ^= x >> 16
    x ^= x >> 8
    x ^= x >> 4
    x ^= x >> 2
    x ^= x >> 1
    return x & 1


def _emit_swd_read(direction: list[int], out: list[int],
                   ap: bool, addr: int) -> int:
    """Append a 46-bit SWD read packet. Returns its bit offset."""
    start = len(direction)
    cmd = _swd_cmd_byte(ap, True, addr)
    for i in range(8):
        direction.append(1)
        out.append((cmd >> i) & 1)
    # 1 TRN + 3 ACK + 32 data + 1 parity + 1 TRN — all target-side.
    for _ in range(_SWD_PACKET_BITS - 8):
        direction.append(0)
        out.append(0)
    return start


def _emit_swd_write(direction: list[int], out: list[int],
                    ap: bool, addr: int, data: int) -> int:
    """Append a 46-bit SWD write packet. Returns its bit offset."""
    start = len(direction)
    cmd = _swd_cmd_byte(ap, False, addr)
    for i in range(8):
        direction.append(1)
        out.append((cmd >> i) & 1)
    # 1 TRN + 3 ACK + 1 TRN: target side.
    for _ in range(5):
        direction.append(0)
        out.append(0)
    # 32 data bits, host-driven.
    data &= 0xFFFFFFFF
    for i in range(32):
        direction.append(1)
        out.append((data >> i) & 1)
    # 1 parity bit, host-driven.
    direction.append(1)
    out.append(_data_parity(data))
    return start


def _extract_ack(in_bits, packet_offset: int) -> int:
    return (in_bits[packet_offset + _ACK_OFFSET]
            | (in_bits[packet_offset + _ACK_OFFSET + 1] << 1)
            | (in_bits[packet_offset + _ACK_OFFSET + 2] << 2))


def _extract_read_data(in_bits, packet_offset: int) -> int:
    val = 0
    for i in range(32):
        if in_bits[packet_offset + _READ_DATA_OFFSET + i]:
            val |= 1 << i
    return val


# ---- The Dp variant ----------------------------------------------

class JLinkSwDp(dpmod.Dp):
    """ARM Debug Port over SWD via J-Link bit-bang."""

    SELECT_REG = 0x08
    RDBUFF_REG = 0x0c
    ABORT_REG  = 0x00  # write-only

    def __init__(self, transport, name: str = "swd"):
        super().__init__(name=name)
        self._transport = transport
        self._select: int | None = None

    async def start(self):
        await self._transport.select_interface(protocol.TIF_SWD)
        await self._transport.deassert_reset()
        await self._transport.set_speed_khz(1000)
        await self._enter_swd()
        await super().start()

    async def _enter_swd(self):
        """Drive the JTAG→SWD switch sequence robustly (mirrors
        crobe's protocol/swd.py line_reset):

        1. ≥250 SWCLK cycles with SWDIO high — line reset 1.
        2. 16-bit switch word (0xE79E LSB-first).
        3. 50 cycles SWDIO high.
        4. ANOTHER 16-bit switch word (handles the case where the
           DP was already in SWD; the second switch is a no-op then).
        5. ≥200 cycles SWDIO high — line reset 2.
        6. ≥1 idle cycle (SWDIO low).

        After this the SW-DP is in the post-reset state and the
        next transaction must be a DPIDR read (which Dp.start does).
        """
        direction = []
        out = []

        def emit(n, v):
            for _ in range(n):
                direction.append(1)
                out.append(v)

        def emit_switch():
            switch = 0xE79E
            for i in range(16):
                direction.append(1)
                out.append((switch >> i) & 1)

        emit(250, 1)         # line reset 1
        emit_switch()        # JTAG → SWD
        emit(50, 1)
        emit_switch()        # idempotent if already SWD
        emit(200, 1)         # line reset 2
        emit(8, 0)           # idle

        await self._transport.swd_io(
            _pack_bits(direction), _pack_bits(out), len(direction))

        # Per ADIv5/v6: after the switch sequence the SW-DP's SELECT
        # register is reset to 0. Pre-seed our cache so the first
        # DpRead(DPIDR) doesn't trigger a redundant SELECT=0 write —
        # which the freshly-reset DP rejects with invalid ACK
        # (the FIRST post-switch transaction must be DPIDR read).
        self._select = 0

    async def _raw_dp_write(self, addr: int, data: int):
        """Single-packet DP write that bypasses the full flush_ops
        pipeline — used for the initial ABORT before Dp.start()."""
        direction = []
        out = []
        offset = _emit_swd_write(direction, out, False, addr, data)
        in_bytes = await self._transport.swd_io(
            _pack_bits(direction), _pack_bits(out), len(direction))
        in_bits = _unpack_bits(in_bytes, len(direction))
        ack = _extract_ack(in_bits, offset)
        if ack != _ACK_OK:
            self.logger.warning(
                "SWD initial ABORT got ACK=0b%03b (continuing)", ack)

    def _select_for(self, op) -> int:
        """SELECT value needed for this op (matches JtagDp's math)."""
        cur = 0 if self._select is None else self._select
        if isinstance(op, (dpmod.ApRead, dpmod.ApWrite)):
            apsel = (op.ap >> 24) & 0xff
            apbank = (op.addr >> 4) & 0xf
            return (apsel << 24) | (apbank << 4) | (cur & 0xf)
        # DP op — preserve AP fields, update DPBANKSEL.
        return (cur & 0xFFFFFFF0) | ((op.addr >> 4) & 0xf)

    async def flush_ops(self, batch):
        direction: list[int] = []
        out: list[int] = []

        # Per-op records:
        # (kind, user_future, ack_offset, data_offset_or_None)
        # kind ∈ { "dp_read", "dp_write", "ap_read", "ap_write" }
        # data_offset is the bit offset of the data field that should
        # resolve user_future. For DP reads it's in the same packet;
        # for AP reads it's in the next packet (filled in later).
        records: list[list] = []
        # Pending AP read whose data lives in the next packet's data
        # slot. We fill its data_offset when we emit that next packet.
        pending: list | None = None

        select = self._select

        def flush_pending_with_rdbuff():
            nonlocal pending
            if pending is None:
                return
            offset = _emit_swd_read(direction, out, False, self.RDBUFF_REG)
            pending[3] = offset + _READ_DATA_OFFSET
            # The RDBUFF read itself doesn't have a user future
            # waiting on its data — only on its ACK so we can detect
            # errors. We piggyback on records via a phantom entry.
            records.append(["rdbuff", None, offset + _ACK_OFFSET, None])
            pending = None

        for op, future in batch:
            if isinstance(op, dpmod.Run):
                for _ in range(op.cycles):
                    direction.append(1)
                    out.append(0)
                future.set_result(None)
                continue

            if isinstance(op, dpmod.Abort):
                offset = _emit_swd_write(direction, out, False,
                                         self.ABORT_REG, op.what)
                records.append(["dp_write", future,
                                offset + _ACK_OFFSET, None])
                continue

            if not isinstance(op, (dpmod.DpRead, dpmod.DpWrite,
                                   dpmod.ApRead, dpmod.ApWrite)):
                future.set_exception(TypeError(
                    f"JLinkSwDp can't lower {type(op).__name__}"))
                continue

            new_select = self._select_for(op)
            if select != new_select:
                # SELECT change breaks the AP read pipeline.
                flush_pending_with_rdbuff()
                offset = _emit_swd_write(direction, out, False,
                                         self.SELECT_REG, new_select)
                records.append(["dp_write", None,
                                offset + _ACK_OFFSET, None])
                select = new_select

            wire_addr = op.addr & 0xc

            if isinstance(op, dpmod.DpRead):
                offset = _emit_swd_read(direction, out, False, wire_addr)
                records.append(["dp_read", future,
                                offset + _ACK_OFFSET,
                                offset + _READ_DATA_OFFSET])

            elif isinstance(op, dpmod.DpWrite):
                offset = _emit_swd_write(direction, out, False,
                                         wire_addr, op.data)
                records.append(["dp_write", future,
                                offset + _ACK_OFFSET, None])

            elif isinstance(op, dpmod.ApRead):
                offset = _emit_swd_read(direction, out, True, wire_addr)
                # Previous pending AP read's data is in THIS packet's
                # data field.
                if pending is not None:
                    pending[3] = offset + _READ_DATA_OFFSET
                    pending = None
                # This op's ACK is in THIS packet; its data is in
                # the NEXT packet (filled later).
                this_record = ["ap_read", future,
                               offset + _ACK_OFFSET, None]
                records.append(this_record)
                pending = this_record

            else:  # ApWrite
                offset = _emit_swd_write(direction, out, True,
                                         wire_addr, op.data)
                records.append(["ap_write", future,
                                offset + _ACK_OFFSET, None])
                # AP writes don't update RDBUFF — pending stays.

        # End-of-batch: drain trailing pending AP read.
        flush_pending_with_rdbuff()

        self._select = select

        if not direction:
            return

        try:
            in_bytes = await self._transport.swd_io(
                _pack_bits(direction), _pack_bits(out), len(direction))
        except Exception as exc:
            for rec in records:
                if rec[1] is not None and not rec[1].done():
                    rec[1].set_exception(exc)
            raise

        in_bits = _unpack_bits(in_bytes, len(direction))

        for kind, fut, ack_offset, data_offset in records:
            ack = (in_bits[ack_offset]
                   | (in_bits[ack_offset + 1] << 1)
                   | (in_bits[ack_offset + 2] << 2))
            if fut is None:
                if ack != _ACK_OK:
                    self.logger.warning(
                        "SWD %s packet ACK=0b%03b", kind, ack)
                continue
            if fut.done():
                continue
            if ack == _ACK_OK:
                if data_offset is None:
                    fut.set_result(None)
                else:
                    val = 0
                    for i in range(32):
                        if in_bits[data_offset + i]:
                            val |= 1 << i
                    fut.set_result(val)
            elif ack == _ACK_WAIT:
                fut.set_exception(dpmod.DpAccessFailure(
                    f"SWD WAIT (retry not implemented)"))
            elif ack == _ACK_FAULT:
                fut.set_exception(dpmod.DpAccessFailure("SWD FAULT"))
            else:
                fut.set_exception(dpmod.DpAccessFailure(
                    f"SWD invalid ACK 0b{ack:03b}"))
