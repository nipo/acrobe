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

The wire layer is deliberately raw: each ``swd.Read`` / ``swd.Write``
turns into exactly one wire packet, and the returned future
resolves with whatever the chip drove on that packet's data slot.
AP-read pipelining is an ADI SW-DP concern, not a wire concern —
the bookkeeping lives in
:class:`acrobe.component.arm.sw_dp.SwDp`'s lowering."""

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


def _emit_const(direction, out, count: int, value: int) -> None:
    """Append ``count`` host-driven bits all equal to ``value``."""
    for _ in range(count):
        direction.append(1)
        out.append(value)


def _emit_int(direction, out, value: int, count: int) -> None:
    """Append ``count`` host-driven bits of ``value`` LSB-first."""
    for i in range(count):
        direction.append(1)
        out.append((value >> i) & 1)


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
        self.__transport = transport

    async def start(self):
        """Switch the J-Link to SWD mode and bring the line up.

        Deasserts nRST eagerly (some adapters power up with reset
        asserted, masking the target's SWDIO driver) and sets a
        sensible default speed, then defers to
        :meth:`swd.Interface.start` for the chip-side wakeup (line
        reset + JTAG-to-SWD switch + DPIDR read + DP spawn)."""
        await self.__transport.select_interface(protocol.TIF_SWD)
        await self.__transport.deassert_reset()
        await self.__transport.set_speed_khz(1000)
        await super().start()

    # Cap the per-chunk SWD bit-bang size: large batched mem-AP
    # blob ops otherwise build a single swd_io request the device
    # firmware can't service. Empirically a Silicon Labs J-Link OB
    # silently truncates responses above ~4 KiB; 8192 bits
    # (≈1024 bytes per direction/out, plus a like-sized response)
    # sits well within that window. Larger batches are split into
    # multiple swd_io calls at SWD transaction boundaries — never
    # mid-transaction, so the target sees only inter-transaction
    # idle cycles between chunks.
    MAX_CHUNK_BITS = 8192

    async def flush_wire_ops(self, batch):
        direction: list[int] = []
        out: list[int] = []
        # Per-packet record: [user_future, kind, ack_offset,
        #                     data_offset_or_None]. Each record covers
        # exactly one wire packet — no pipeline awareness.
        records: list[list] = []

        async def issue_chunk():
            """Emit the accumulated direction/out as one swd_io and
            resolve every future in `records`. Chunks split at
            transaction boundaries; the chip-side AP-read latch is
            preserved across chunks (the chip stays in the same SWD
            state across short idle periods)."""
            nonlocal direction, out, records
            if not direction:
                return
            try:
                in_bytes = await self.__transport.swd_io(
                    _pack_bits(direction), _pack_bits(out),
                    len(direction))
            except Exception as exc:
                for rec in records:
                    if rec[0] is not None and not rec[0].done():
                        rec[0].set_exception(exc)
                raise
            in_bits = _unpack_bits(in_bytes, len(direction))
            self.__resolve_records(records, in_bits)
            direction = []
            out = []
            records = []

        for op, future in batch:
            # Cap accumulated bit-bang size between transactions —
            # `issue_chunk` resolves accumulated records and clears
            # arrays.
            if len(direction) >= self.MAX_CHUNK_BITS:
                await issue_chunk()
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
                self.__emit_jtag_to_swd(direction, out)
                future.set_result(None)
                continue

            if isinstance(op, swd.SwdToDormant):
                _emit_const(direction, out, 60, 1)
                _emit_int(direction, out, 0xE3BC, 16)
                future.set_result(None)
                continue

            if isinstance(op, swd.DormantToSwd):
                _emit_const(direction, out, 8, 1)
                _emit_int(direction, out, 0x86852D956209F392, 64)
                _emit_int(direction, out, 0x19BC0EA2E3DDAFE9, 64)
                _emit_const(direction, out, 4, 0)
                _emit_int(direction, out, 0x1A, 8)
                future.set_result(None)
                continue

            if isinstance(op, swd.TargetSelWrite):
                # Bit-bang the entire transaction (no ACK capture):
                # cmd + TRN + 3 ACK cycles (host high) + TRN + data +
                # parity + 8 idle. Per spec no DP responds.
                cmd = _swd_cmd_byte(False, False, 0x0c)
                _emit_int(direction, out, cmd, 8)
                _emit_const(direction, out, 5, 1)
                data = op.target & 0xFFFFFFFF
                _emit_int(direction, out, data, 32)
                _emit_int(direction, out, _data_parity(data), 1)
                _emit_const(direction, out, 8, 0)
                future.set_result(None)
                continue

            if isinstance(op, swd.Read):
                # One packet, one future resolving with whatever the
                # chip drove on its data slot. AP-read pipeline
                # interpretation lives in SwDp.flush_ops.
                offset = _emit_swd_read(direction, out, op.ap, op.addr)
                kind = "ap_read" if op.ap else "dp_read"
                records.append([future, kind,
                                offset + _ACK_OFFSET,
                                offset + _READ_DATA_OFFSET])
                continue

            if isinstance(op, swd.Write):
                offset = _emit_swd_write(direction, out,
                                         op.ap, op.addr, op.data)
                kind = "ap_write" if op.ap else "dp_write"
                records.append([future, kind, offset + _ACK_OFFSET, None])
                continue

            future.set_exception(TypeError(
                f"JLinkSwdInterface can't lower {type(op).__name__}"))

        await issue_chunk()

    def __resolve_records(self, records, in_bits):
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
    def __emit_jtag_to_swd(direction, out):
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
