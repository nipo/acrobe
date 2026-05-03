"""J-Link SWD wire interface.

Implements :class:`acrobe.protocol.swd.Interface` on top of the
J-Link's bit-bang ``CMD_SWD_IO`` (opcode 0xCF when TIF=SWD). One
USB transaction per :meth:`flush_ops` batch — the underlying
batcher accumulates enough work to amortise the round-trip cost.

Wire-protocol notes
-------------------

The J-Link firmware exposes the SWD packet *minus* the TRN
turnaround cycles: ACK starts at offset 8 (right after the 8 cmd
bits), then 32 data bits, then parity, then a couple of trailing
bits. OpenOCD's ``jlink_swd_queue_cmd`` reads ACK at the same
offset; matching that convention avoids surprises.

AP-read pipelining is part of the wire protocol: the data of an
AP read packet lands in the data field of the *next* packet. We
keep a ``pending`` slot for the in-flight AP read and fill its
``data_offset`` when the next packet (read OR write) is queued. At
end of batch we drain a trailing pending read with an explicit
RDBUFF read."""

from __future__ import annotations

from ...protocol import swd
from . import protocol


# ---- Bit-pack helpers (LSB-first) ---------------------------------

def _pack_bits(bits) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _unpack_bits(data: bytes, count: int):
    return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]


# ---- SWD packet primitives ----------------------------------------
#
#   READ:  cmd[0..7]  ACK[8..10]  data[11..42]  parity[43]
#   WRITE: cmd[0..7]  ACK[8..10]  (TRN-padding)  data[..]  parity[..]
#
# ACK encoding: ACK[0] is the bit at offset 8 — first bit on the wire.

_ACK_OFFSET       = 8
_READ_DATA_OFFSET = 11

_ACK_OK    = swd.Ack.OK
_ACK_WAIT  = swd.Ack.WAIT
_ACK_FAULT = swd.Ack.FAULT

# RDBUFF DP register — read here to drain a pending AP read at end
# of batch.
_RDBUFF_REG = 0x0c


def _swd_cmd_byte(ap: bool, rnw: bool, addr: int) -> int:
    """8-bit SWD request packet header (LSB-first wire order):

        bit 0: start (1)
        bit 1: APnDP
        bit 2: RnW
        bit 3: A[2]
        bit 4: A[3]
        bit 5: parity (even over bits 1..4)
        bit 6: stop (0)
        bit 7: park (1)
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


def _emit_swd_read(direction, out, ap: bool, addr: int) -> int:
    """Append a 46-bit SWD read packet. Returns its bit offset.

    8 cmd bits (host) + 38 in-bits (target) — same total as crobe and
    OpenOCD's jlink_swd_queue_cmd. The 38 in-bits cover ACK + DATA +
    PARITY + the trailing TRN/idle padding the firmware exposes."""
    start = len(direction)
    cmd = _swd_cmd_byte(ap, True, addr)
    for i in range(8):
        direction.append(1)
        out.append((cmd >> i) & 1)
    for _ in range(38):
        direction.append(0)
        out.append(0)
    return start


def _emit_swd_write(direction, out, ap: bool, addr: int, data: int) -> int:
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


# ---- The J-Link SWD interface -------------------------------------


class JLinkSwdInterface(swd.Interface):
    """SWD wire interface over the J-Link's bit-bang SWD_IO command."""

    def __init__(self, transport, name: str = "swd"):
        super().__init__(name=name)
        self._transport = transport

    async def start(self):
        """Switch the J-Link to SWD mode and bring the line up.

        We deassert nRST eagerly (some adapters power up with reset
        asserted, masking the target's SWDIO driver) and set a
        sensible default speed. The chip-side wakeup (line reset +
        JTAG-to-SWD switch + idle) is the SwDp's job — it posts
        :class:`swd.JtagToSwd` from its own ``start()``."""
        await self._transport.select_interface(protocol.TIF_SWD)
        await self._transport.deassert_reset()
        await self._transport.set_speed_khz(1000)

    async def flush_ops(self, batch):
        direction: list[int] = []
        out: list[int] = []
        # Per-packet record: [user_future, kind, ack_offset,
        #                     data_offset_or_None]
        # Some entries have user_future=None (phantom RDBUFF).
        records: list[list] = []
        # Pending AP read whose data lives in the *next* packet's
        # data slot. When we emit a new read, we fill this entry's
        # data_offset and clear pending.
        pending: list | None = None

        def flush_pending_with_rdbuff():
            nonlocal pending
            if pending is None:
                return
            offset = _emit_swd_read(direction, out, False, _RDBUFF_REG)
            pending[3] = offset + _READ_DATA_OFFSET
            records.append([None, "rdbuff", offset + _ACK_OFFSET, None])
            pending = None

        for op, future in batch:
            if isinstance(op, swd.Run):
                for _ in range(op.cycles):
                    direction.append(1)
                    out.append(0)
                future.set_result(None)
                continue

            if isinstance(op, swd.Wakeup):
                for _ in range(op.cycles):
                    direction.append(1)
                    out.append(1)
                future.set_result(None)
                continue

            if isinstance(op, swd.LineReset):
                # 60 cycles SWDIO=1 + 8 idle cycles — comfortably
                # over the spec minimum (50 + 2).
                for _ in range(60):
                    direction.append(1)
                    out.append(1)
                for _ in range(8):
                    direction.append(1)
                    out.append(0)
                future.set_result(None)
                continue

            if isinstance(op, swd.JtagToSwd):
                self._emit_jtag_to_swd(direction, out)
                future.set_result(None)
                continue

            if isinstance(op, swd.Read):
                offset = _emit_swd_read(direction, out, op.ap, op.addr)
                if op.ap:
                    # AP read: ACK in this packet, data in NEXT packet.
                    if pending is not None:
                        # Previous AP read's data is in THIS packet's
                        # data field.
                        pending[3] = offset + _READ_DATA_OFFSET
                        pending = None
                    rec = [future, "ap_read", offset + _ACK_OFFSET, None]
                    records.append(rec)
                    pending = rec
                else:
                    # DP read: ACK + data both in this packet.
                    if pending is not None:
                        pending[3] = offset + _READ_DATA_OFFSET
                        pending = None
                    rec = [future, "dp_read",
                           offset + _ACK_OFFSET,
                           offset + _READ_DATA_OFFSET]
                    records.append(rec)
                continue

            if isinstance(op, swd.Write):
                offset = _emit_swd_write(direction, out,
                                         op.ap, op.addr, op.data)
                kind = "ap_write" if op.ap else "dp_write"
                # Writes don't update RDBUFF — pending stays.
                records.append([future, kind, offset + _ACK_OFFSET, None])
                continue

            future.set_exception(TypeError(
                f"JLinkSwdInterface can't lower {type(op).__name__}"))

        # Drain any trailing pending AP read with an explicit RDBUFF
        # read so the caller's future resolves before this batch ends.
        flush_pending_with_rdbuff()

        if not direction:
            return

        try:
            in_bytes = await self._transport.swd_io(
                _pack_bits(direction), _pack_bits(out), len(direction))
        except Exception as exc:
            for rec in records:
                if rec[0] is not None and not rec[0].done():
                    rec[0].set_exception(exc)
            raise

        in_bits = _unpack_bits(in_bytes, len(direction))

        for fut, kind, ack_offset, data_offset in records:
            ack = (in_bits[ack_offset]
                   | (in_bits[ack_offset + 1] << 1)
                   | (in_bits[ack_offset + 2] << 2))
            if fut is None:
                if ack != _ACK_OK:
                    self.logger.warning(
                        "SWD %s packet ACK=0b%s",
                        kind, format(ack, "03b"))
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
                fut.set_exception(swd.SwdWait(
                    f"WAIT on {kind} (retry not implemented)"))
            elif ack == _ACK_FAULT:
                fut.set_exception(swd.SwdAccessFailure(f"FAULT on {kind}"))
            else:
                fut.set_exception(swd.SwdAccessFailure(
                    f"invalid ACK 0b{ack:03b} on {kind}"))

    @staticmethod
    def _emit_jtag_to_swd(direction, out):
        """Append the JTAG→SWD switch sequence (matches crobe).

            1. ≥250 cycles SWDIO=1 (line reset)
            2. 16-bit switch (0xE79E LSB-first / 0x79E7 MSB-first)
            3. ≥50 cycles SWDIO=1
            4. Another 16-bit switch (no-op if already in SWD)
            5. ≥200 cycles SWDIO=1 (line reset)
            6. ≥2 idle cycles SWDIO=0
        """
        def emit(n, v):
            for _ in range(n):
                direction.append(1)
                out.append(v)

        def emit_switch():
            sw = 0xE79E
            for i in range(16):
                direction.append(1)
                out.append((sw >> i) & 1)

        emit(250, 1)
        emit_switch()
        emit(50, 1)
        emit_switch()
        emit(200, 1)
        emit(16, 0)
