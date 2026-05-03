"""ARM JTAG-DP: TAP and DP overlay.

Two Nodes work together:

* :class:`JtagDpTap` — a JTAG TAP registered against ``Tap.db`` that
  the chain discovery layer instantiates when it sees a JTAG-DP
  IDCODE. Defines the four JTAG-DP IR instructions and their DR
  shapes.
* :class:`JtagDp` — the ``Dp`` overlay added as a child of the TAP at
  ``start()``. Lowers DP/AP ops to DPACC/APACC 35-bit shifts on the
  parent TAP, manages SELECT caching, and implements the pending-read
  scheme: AP reads' responses ride the *next* shift's TDO, with a
  forced RDBUFF flush at end of batch.

Bookkeeping for pending reads lives in local dicts inside
``flush_ops`` — never on the op dataclasses (which are frozen and
re-postable).
"""

from __future__ import annotations

import asyncio

from ...bitstring import BitString
from ...protocol.jtag import Dr, Instruction, Tap
from . import dp as dpmod


# --- 35-bit DPACC / APACC packing helpers --------------------------

class _Wire:
    """Packing/unpacking for the 35-bit DR shift used by DPACC, APACC,
    and ABORT.

    Request layout (TDI, LSB first):
        bit 0      RnW (1 = read, 0 = write)
        bits 2:1   register select (addr[3:2])
        bits 34:3  data (32 bits, LSB first)

    Response layout (TDO, LSB first):
        bits 2:0   ACK (010 = OK_OR_FAULT, 001 = WAIT)
        bits 34:3  read data of the previously-shifted request
    """

    ACK_OK_FAULT = 0b010
    ACK_WAIT     = 0b001

    @staticmethod
    def pack(rnw: bool, addr: int, data: int = 0) -> int:
        return ((data & 0xffffffff) << 3) | ((addr & 0xc) >> 1) | (1 if rnw else 0)

    @staticmethod
    def unpack(tdo: BitString) -> tuple[int, int]:
        v = int(tdo)
        ack = v & 0x7
        data = (v >> 3) & 0xffffffff
        return ack, data


# --- JTAG-DP TAP ---------------------------------------------------

class JtagDpTap(Tap):
    """JTAG-DP TAP. Owns the four JTAG-DP IR instructions and a
    :class:`JtagDp` child that exposes the DP/AP register space.

    Vendor-specific TAPs that share the JTAG-DP IDCODE pattern can
    subclass this and add their own instructions; the DP child still
    operates the same."""

    irlen = 4
    max_freq = 20e6

    DPACC_DR  = Dr(35)
    APACC_DR  = Dr(35)
    ABORT_DR  = Dr(35)
    IDCODE_DR = Dr(32)

    DPACC    = Instruction(0xa, "DPACC_DR")
    APACC    = Instruction(0xb, "APACC_DR")
    ABORT_IR = Instruction(0x8, "ABORT_DR")
    IDCODE   = Instruction(0xe, "IDCODE_DR")

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "JTAG-DP"
        super().__init__(idcode=idcode, irlen=irlen, name=name)

    async def start(self):
        self.child_add(JtagDp())


# --- JTAG-DP IDCODE registrations ---------------------------------
#
# Tap.db's equality function masks the revision nibble (bits 31:28),
# so we register one IDCODE per JEP106+part variant. Bits 27:12 are
# the part number, bits 11:1 are JEDEC ID (with JEP106 bank in the
# high bits), bit 0 is 1.
#
# Known JTAG-DP IDCODEs (ARM JEP106 = 0x23B, low half = 0x477):
#   0x_BA00477  — JTAG-DPv0/v1 (ADIv5)
#   0x_BA01477  — JTAG-DPv1 multidrop variant
#   0x_BA02477  — JTAG-DPv2 (ADIv5 with multidrop)
#   0x_BA05477  — JTAG-DPv5 (ADIv6, observed on Cortex-A devices)

JTAG_DP_IDCODES = (
    0x0BA00477,
    0x0BA01477,
    0x0BA02477,
    0x0BA05477,
)

for _idcode in JTAG_DP_IDCODES:
    Tap.db.register(_idcode)(JtagDpTap)


# --- DP overlay ----------------------------------------------------

class JtagDp(dpmod.Dp):
    """ARM Debug Port over JTAG. Translates batched DP/AP ops to
    DPACC/APACC shifts on the parent :class:`JtagDpTap`."""

    # Idle TCKs between consecutive DPACC/APACC DR shifts. JTAG-DP
    # needs some time after Update-DR to perform the underlying access
    # before the next Capture-DR; without this, every other access
    # WAITs.
    INTER_SHIFT_RUN = 8

    def __init__(self, name: str = "dap"):
        super().__init__(name)
        self._select: int | None = None  # cached SELECT value

    def _select_for(self, op) -> int:
        """Compute the SELECT value needed for ``op``. Bits 31:24 =
        APSEL, bits 7:4 = APBANKSEL, bits 3:0 = DPBANKSEL."""
        cur = 0 if self._select is None else self._select
        if isinstance(op, (dpmod.ApRead, dpmod.ApWrite)):
            apsel = (op.ap >> 24) & 0xff
            apbank = (op.addr >> 4) & 0xf
            dpbank = cur & 0xf
        else:
            apsel = (cur >> 24) & 0xff
            apbank = (cur >> 4) & 0xf
            dpbank = (op.addr >> 4) & 0xf
        return (apsel << 24) | (apbank << 4) | dpbank

    async def flush_ops(self, batch):
        tap = self._parent
        if tap is None or not isinstance(tap, JtagDpTap):
            raise RuntimeError(
                f"JtagDp {self.name!r}: parent must be a JtagDpTap, got {tap!r}")

        # All bit-level shift futures (we await them all together).
        shift_futures: list[asyncio.Future] = []
        # Pairs of (shift_future_carrying_response, user_future_to_resolve).
        result_shifts: list[tuple[asyncio.Future, asyncio.Future]] = []
        # User future whose response is in flight (to be picked up by
        # the next shift's TDO, or a forced RDBUFF read at end-of-batch).
        pending_user_future: asyncio.Future | None = None

        select = self._select

        def emit_idle():
            shift_futures.append(tap.run(self.INTER_SHIFT_RUN))

        def emit_dpacc(rnw: bool, addr: int, data: int = 0,
                       capture: bool = True) -> asyncio.Future:
            tdi = BitString(_Wire.pack(rnw, addr, data), 35)
            f = tap.DPACC(tdi, read_tdo=capture)
            shift_futures.append(f)
            emit_idle()
            return f

        def emit_apacc(rnw: bool, addr: int, data: int = 0,
                       capture: bool = True) -> asyncio.Future:
            tdi = BitString(_Wire.pack(rnw, addr, data), 35)
            f = tap.APACC(tdi, read_tdo=capture)
            shift_futures.append(f)
            emit_idle()
            return f

        def flush_pending_via_rdbuff():
            nonlocal pending_user_future
            if pending_user_future is None:
                return
            f = emit_dpacc(rnw=True, addr=dpmod.Dp.RDBUFF, capture=True)
            result_shifts.append((f, pending_user_future))
            pending_user_future = None

        for op, user_future in batch:
            if isinstance(op, dpmod.Run):
                shift_futures.append(tap.run(op.cycles))
                user_future.set_result(None)
                continue

            if isinstance(op, dpmod.Abort):
                # ABORT IR + 35-bit DR shift; data is left-shifted
                # by 3 to land in the data field (RnW + addr fields
                # are ignored by the abort path).
                tdi = BitString((op.what & 0xffffffff) << 3, 35)
                f = tap.ABORT_IR(tdi, read_tdo=False)
                shift_futures.append(f)
                emit_idle()
                user_future.set_result(None)
                continue

            new_select = self._select_for(op)
            if select != new_select:
                # SELECT-write breaks the AP-read pipeline, so flush
                # any pending response first.
                flush_pending_via_rdbuff()
                emit_dpacc(rnw=False, addr=dpmod.Dp.SELECT,
                           data=new_select, capture=False)
                select = new_select

            wire_addr = op.addr & 0xc

            if isinstance(op, dpmod.ApRead):
                f = emit_apacc(rnw=True, addr=wire_addr, capture=True)
            elif isinstance(op, dpmod.ApWrite):
                f = emit_apacc(rnw=False, addr=wire_addr,
                               data=op.data, capture=True)
            elif isinstance(op, dpmod.DpRead):
                f = emit_dpacc(rnw=True, addr=wire_addr, capture=True)
            elif isinstance(op, dpmod.DpWrite):
                f = emit_dpacc(rnw=False, addr=wire_addr,
                               data=op.data, capture=True)
            else:
                user_future.set_exception(
                    TypeError(f"Unhandled DP op: {type(op).__name__}"))
                continue

            # The shift we just emitted carries the TDO of the
            # *previous* request — hand it to the pending user future.
            if pending_user_future is not None:
                result_shifts.append((f, pending_user_future))
                pending_user_future = None

            # If the current op is a read, mark its user future as the
            # next pending one (its data will arrive on the next shift).
            if isinstance(op, (dpmod.ApRead, dpmod.DpRead)):
                pending_user_future = user_future
            else:
                user_future.set_result(None)

        # End-of-batch: drain any trailing pending read with RDBUFF.
        flush_pending_via_rdbuff()

        self._select = select

        if shift_futures:
            await asyncio.gather(*shift_futures)

        # Resolve user futures from the shifts that carry their
        # responses. ACK and read-data are extracted here.
        for shift_f, user_f in result_shifts:
            if user_f.done():
                continue
            tdo = shift_f.result()
            ack, data = _Wire.unpack(tdo)
            if ack == _Wire.ACK_OK_FAULT:
                user_f.set_result(data)
            elif ack == _Wire.ACK_WAIT:
                user_f.set_exception(dpmod.DpAccessFailure(
                    "JTAG-DP WAIT response (retry not yet implemented)"))
            else:
                user_f.set_exception(dpmod.DpAccessFailure(
                    f"JTAG-DP invalid ACK 0b{ack:03b}"))
