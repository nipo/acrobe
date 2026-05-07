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
import functools
from ...bitstring import BitString
from ...part_id import PartId
from ...protocol.jtag import Dr, Instruction, Tap
from . import dp as dpmod

class _Wire:
    """Packing/unpacking for the 35-bit DR shift used by DPACC, APACC,
    and ABORT.

    Request layout (TDI, LSB first):
        bit 0      RnW (1 = read, 0 = write)
        bits 2:1   register select (addr[3:2])
        bits 34:3  data (32 bits, LSB first)

    Response layout (TDO, LSB first):
        bits 2:0   ACK
        bits 34:3  read data of the previously-shifted request

    ACK encoding depends on the JTAG-DP protocol version (DPIDR.DPVER
    selects, but the IDCODE alone tells us which encoding to use —
    distinct part numbers per protocol version):

      Protocol v0 (DPv0/v1/v2, ADIv5):
        0b001 = WAIT, 0b010 = OK_OR_FAULT
      Protocol v1 (DPv3, ADIv6):
        0b001 = WAIT, 0b010 = FAULT, 0b100 = OK
    """

    # Protocol v0 — also re-used as "any non-WAIT response" for callers
    # that share v0/v1 bookkeeping (kept as class-level constants for
    # legacy tests).
    ACK_OK_FAULT = 0b010
    ACK_WAIT     = 0b001

    # Protocol v1 (ADIv6 / DPv3) split.
    ACK_V1_OK    = 0b100
    ACK_V1_FAULT = 0b010

    @staticmethod
    def pack(rnw: bool, addr: int, data: int = 0) -> int:
        return ((data & 0xffffffff) << 3) | ((addr & 0xc) >> 1) | (1 if rnw else 0)

    @staticmethod
    def unpack(tdo: BitString) -> tuple[int, int]:
        v = int(tdo)
        ack = v & 0x7
        data = (v >> 3) & 0xffffffff
        return ack, data


@Tap.db.register(PartId.from_idcode(0x0BA00477))
@Tap.db.register(PartId.from_idcode(0x0BA01477))
@Tap.db.register(PartId.from_idcode(0x0BA02477))
class JtagDpTap(Tap):
    """JTAG-DP TAP. Owns the four JTAG-DP IR instructions and a
    :class:`JtagDp` child that exposes the DP/AP register space.

    Vendor-specific TAPs that share the JTAG-DP IDCODE pattern can
    subclass this and add their own instructions; the DP child still
    operates the same.

    ``JTAG_PROTOCOL_VERSION`` selects the wire-level ACK encoding
    used by the :class:`JtagDp` child:

      * ``0`` — DPv0/v1/v2 (ADIv5). Default.
      * ``1`` — DPv3 (ADIv6). See :class:`JtagDpV3Tap`.

    IR opcodes, DR widths, and SELECT layout are identical between
    the two — only the ACK decoding differs."""

    irlen = 4
    max_freq = 20e6
    JTAG_PROTOCOL_VERSION = 0

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
        self.child_add(JtagDp(jtag_protocol_version=self.JTAG_PROTOCOL_VERSION))

@Tap.db.register(PartId.from_idcode(0x0BA06477))
class JtagDpV3Tap(JtagDpTap):
    """JTAG-DP using JTAG protocol version 1 (DPv3 / ADIv6).

    Wire-level differences from :class:`JtagDpTap`:

      * ACK encoding: ``0b001=WAIT``, ``0b010=FAULT``, ``0b100=OK``
        (versus protocol v0: ``0b001=WAIT``, ``0b010=OK_OR_FAULT``).
      * IR opcodes, DR widths, and SELECT layout are identical.

    ADIv6-specific behaviours that live above the wire (AP enumeration
    via BASEPTR, APv2 register layout) are handled by the DP / AP
    layers — not here."""

    JTAG_PROTOCOL_VERSION = 1

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "JTAG-DPv3"
        super().__init__(idcode=idcode, irlen=irlen, name=name)

class JtagDpLowerer:
    """Object instantiated every time we need to lower a batch of DP
    operations down to JTAG layer.

    Keeps track of select and pending reads where data is attached to
    subsequent DPACC or ACACC shifts.
    """
    
    # Idle TCKs between consecutive APACC DR shifts.
    INTER_AP_RUN = 8

    def __init__(self, version: int, tap: JtagDpTap):
        self.version = version
        self.tap = tap

        self.last_select = None

        self.pending = None

    # Future handling
        
    def chain_completion(self, upper: asyncio.Future, lower: asyncio.Future):
        """
        Hook `lower` future done callback to resolve `upper`
        """
        lower.add_done_callback(functools.partial(self._completion_from_lower, upper))

    def chain_data(self, upper: asyncio.Future, lower: asyncio.Future):
        """Hook `lower` future done callback to resolve `upper` with
        response data.
        """
        lower.add_done_callback(functools.partial(self._data_from_lower, upper))

    def _completion_from_lower(self, upper: asyncio.Future, lower: asyncio.Future):
        """
        Actual implementation for chain_completion()
        """
        try:
            upper.set_result(lower.result())
        except Exception as e:
            upper.set_exception(e)

    def _data_from_lower(self, upper: asyncio.Future, lower: asyncio.Future):
        """
        Actual implementation for chain_data()
        """
        try:
            tdo = lower.result()
        except Exception as e:
            upper.set_exception(e)
            return
        ack, data = _Wire.unpack(tdo)
        if ack == _Wire.ACK_WAIT:
            upper.set_exception(dpmod.DpAccessFailure("wait"))
        elif (self.version == 0 and ack == _Wire.ACK_OK_FAULT) \
             or ack == _Wire.ACK_V1_OK:
            upper.set_result(data)
        else:
            upper.set_exception(dpmod.DpAccessFailure("fault"))

    # Low-level shifts
            
    def dp_access(self, read: bool, address: int, data: int):
        """
        Low-level DPACC shift, no upper address update.
        """

        acc = self.tap.DPACC(_Wire.pack(read, address, data),
                             read_tdo = self.pending is not None)
        self.tap.run(42)
        if self.pending:
            self.chain_data(self.pending, acc)
            self.pending = None

    def ap_access(self, read: bool, address: int, data: int):
        """
        Low-level APACC shift, no upper address update.
        """
        lower = self.tap.APACC(_Wire.pack(read, address, data),
                               read_tdo = self.pending is not None)
#        self.tap.run(self.INTER_AP_RUN)
        self.tap.run(42)
        if self.pending:
            self.chain_data(self.pending, lower)
            self.pending = None

    # Book keeping

    def ap_select(self, address: int):
        """
        Change AP address higher bits.
        Noop if not actually changing
        """
        address &= 0xfffffff0
        self.select(address | ((self.last_select or 0) & 0xf))

    def dp_select(self, address: int, read: bool):
        """
        Change DP address higher bits.
        Noop if not actually changing
        Noop if accessed register is present in all banks
        """
        dp_low = (address & 0xc)
        if dp_low == dpmod.Dp.RDBUFF:
            return
        if not read and dp_low == dpmod.Dp.SELECT:
            return
        dp_bank = (address >> 4) & 0xf
        self.select(dp_bank | ((self.last_select or 0) & 0xfffffff0))

    def select(self, select) -> asyncio.Future | None:
        """
        Update select, may be a noop if not actually changing.
        Will gather pending DP and AP accesses
        """
        if self.last_select == select:
            return

        self.last_select = select
        self.dp_access(False, dpmod.Dp.SELECT, select)

    def flush(self):
        """
        In pending AP and DP reads, get one
        """
        if self.pending:
            self.dp_access(True, dpmod.Dp.RDBUFF, 0)

    # Operations
            
    def run(self, op: dpmod.Run, pending: asyncio.Future):
        """
        Lowers one Run operation and chains completion to pending
        """
        self.chain_completion(pending, self.tap.run(op.cycles))
            
    def abort(self, op: dpmod.Abort, pending: asyncio.Future):
        """
        Lowers one Abort operation and chains completion to pending
        """
        # ABORT IR + 35-bit DR shift; data left-shifted by 3
        # into the data field (RnW + addr bits are ignored).
        self.chain_completion(pending, self.tap.ABORT_IR(op.what << 3, read_tdo=False))
        self.tap.run(self.INTER_AP_RUN)

    def dp_read_write(self, op, pending):
        address = op.addr
        read = isinstance(op, dpmod.DpRead)
        data = 0 if read else op.data

        self.dp_select(address, read)
        self.dp_access(read, address, data)
        self.pending = pending

    def ap_read_write(self, op, pending):
        address = op.addr
        read = isinstance(op, dpmod.ApRead)
        data = 0 if read else op.data

        self.ap_select(address)
        self.ap_access(read, address, data)
        self.pending = pending
        
    def process(self, batch):
        """
        Perform the lowering for one batch
        """
        for op, result in batch:
            if isinstance(op, dpmod.Run):
                self.run(op, result)
                continue

            if isinstance(op, dpmod.Abort):
                self.abort(op, result)
                continue

            if isinstance(op, (dpmod.ApRead, dpmod.ApWrite)):
                self.ap_read_write(op, result)
                continue

            if isinstance(op, (dpmod.DpRead, dpmod.DpWrite)):
                self.dp_read_write(op, result)
                continue

            result.set_exception(
                TypeError(f"Unhandled DP op: {type(op).__name__}"))
        self.flush()

# --- DP overlay ----------------------------------------------------

class JtagDp(dpmod.Dp):
    """ARM Debug Port over JTAG. Translates batched DP/AP ops to
    DPACC/APACC shifts on the parent :class:`JtagDpTap`."""

    def __init__(self, name: str = "dap", jtag_protocol_version: int = 0):
        super().__init__(name)
        self._select: int | None = None  # cached SELECT value
        if jtag_protocol_version not in (0, 1):
            raise ValueError(
                f"JTAG-DP protocol version must be 0 or 1, "
                f"got {jtag_protocol_version!r}")
        self._jtag_protocol_version = jtag_protocol_version
        
    async def flush_ops(self, batch):
        """Lower a DP/AP batch to JTAG-DP wire shifts."""

        try:
            JtagDpLowerer(self._jtag_protocol_version, self._parent).process(batch)
        except Exception as e:
            import traceback
            traceback.print_exc()
