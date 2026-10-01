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

    A JTAG-DP acknowledges a faulted AP access as OK: the fault only
    sets CTRL/STAT.STICKYERR, after which the DP ignores AP accesses.
    So once the control bits of CTRL/STAT are known (the DP wrote it),
    every batch ends, in the same scan, with a CTRL/STAT read and a
    CTRL/STAT write clearing the sticky flags. Op results are held
    until that read is back, and the whole batch fails if STICKYERR
    was set.
    """

    # Idle TCKs between consecutive APACC DR shifts.
    INTER_AP_RUN = 8

    # Stands for an upper future in the slots of the batch's own
    # CTRL/STAT check.
    CHECK = object()

    def __init__(self, version: int, tap: JtagDpTap,
                 ctrl_stat: int | None = None):
        self.version = version
        self.tap = tap
        # CTRL/STAT control bits to write back when clearing the
        # sticky flags, None until the DP wrote CTRL/STAT.
        self.ctrl_stat = ctrl_stat

        self.last_select = None

        self.pending = None

        # One [upper, result, error] slot per lower completion, in
        # lowering order; upper is CHECK for the batch's own check.
        self.__slots = []
        self.__unsettled = 0
        self.__lowering = True
        self.__check = None

    # Future handling

    def __slot(self, upper):
        slot = [upper, None, None]
        self.__slots.append(slot)
        self.__unsettled += 1
        return slot

    def __settled(self):
        self.__unsettled -= 1
        if not self.__unsettled and not self.__lowering:
            self.__finalize()

    def chain_completion(self, upper, lower: asyncio.Future):
        """
        Hook `lower` future done callback to settle `upper`
        """
        lower.add_done_callback(functools.partial(
            self.__completion_from_lower, self.__slot(upper)))

    def chain_data(self, upper, lower: asyncio.Future):
        """Hook `lower` future done callback to settle `upper` with
        response data. Returns the slot settled.
        """
        slot = self.__slot(upper)
        lower.add_done_callback(functools.partial(
            self.__data_from_lower, slot))
        return slot

    def __completion_from_lower(self, slot, lower: asyncio.Future):
        """
        Actual implementation for chain_completion()
        """
        try:
            slot[1] = lower.result()
        except Exception as e:
            slot[2] = e
        self.__settled()

    def __data_from_lower(self, slot, lower: asyncio.Future):
        """
        Actual implementation for chain_data()
        """
        try:
            tdo = lower.result()
        except Exception as e:
            slot[2] = e
        else:
            ack, data = _Wire.unpack(tdo)
            if ack == _Wire.ACK_WAIT:
                slot[2] = dpmod.DpAccessFailure("wait")
            elif (self.version == 0 and ack == _Wire.ACK_OK_FAULT) \
                 or ack == _Wire.ACK_V1_OK:
                slot[1] = data
            else:
                slot[2] = dpmod.DpAccessFailure("fault")
        self.__settled()

    def __finalize(self):
        error = None
        if self.__check is not None:
            stat, clear = self.__check
            error = stat[2] or clear[2]
            if error is None and stat[1] & dpmod.Dp.STICKYERR:
                error = dpmod.DpAccessFailure(
                    f"sticky error (CTRL/STAT 0x{stat[1]:08x})")
        for upper, result, exc in self.__slots:
            if upper is None or upper is self.CHECK or upper.done():
                continue
            if error is not None:
                upper.set_exception(error)
            elif exc is not None:
                upper.set_exception(exc)
            else:
                upper.set_result(result)

    # Low-level shifts

    def dp_access(self, read: bool, address: int, data: int):
        """
        Low-level DPACC shift, no upper address update.
        """

        acc = self.tap.DPACC(_Wire.pack(read, address, data),
                             read_tdo = self.pending is not None)
        if self.pending is not None:
            slot = self.chain_data(self.pending, acc)
            self.pending = None
            return slot
        return None

    def ap_access(self, read: bool, address: int, data: int):
        """
        Low-level APACC shift, no upper address update.

        The AP needs ``INTER_AP_RUN`` idle TCKs in Run-Test/Idle
        between successive APACC shifts. We bake that into the
        APACC's ``post_dr_run`` so the adapter folds those cycles
        into the same MPSSE submission instead of queuing a separate
        ``Tap.run()`` op cascading through every layer.
        """
        lower = self.tap.APACC(_Wire.pack(read, address, data),
                               read_tdo = self.pending is not None,
                               post_dr_run = self.INTER_AP_RUN)
        if self.pending is not None:
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
        if self.pending is not None:
            self.dp_access(True, dpmod.Dp.RDBUFF, 0)

    # Operations

    def run(self, op: dpmod.Run, pending):
        """
        Lowers one Run operation and chains completion to pending
        """
        lower = self.tap.run(op.cycles)
        if pending is not None:
            self.chain_completion(pending, lower)

    def abort(self, op: dpmod.Abort, pending):
        """
        Lowers one Abort operation and chains completion to pending
        """
        # ABORT IR + 35-bit DR shift; data left-shifted by 3
        # into the data field (RnW + addr bits are ignored).
        lower = self.tap.ABORT_IR(op.what << 3, read_tdo=False)
        if pending is not None:
            self.chain_completion(pending, lower)
        self.tap.run(self.INTER_AP_RUN)

    def dp_read_write(self, op, pending):
        address = op.addr
        read = isinstance(op, dpmod.DpRead)
        data = 0 if read else op.data

        if not read and address == dpmod.Dp.CTRL_STAT:
            self.ctrl_stat = data & ~dpmod.Dp.STICKY_MASK

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

    def sticky_check(self):
        """
        Read CTRL/STAT, then clear its sticky flags, keeping the
        control bits last written.
        """
        ctrl_stat = dpmod.Dp.CTRL_STAT
        self.dp_select(ctrl_stat, True)
        self.dp_access(True, ctrl_stat, 0)
        self.pending = self.CHECK
        stat = self.dp_access(False, ctrl_stat,
                              self.ctrl_stat | dpmod.Dp.STICKY_MASK)
        self.pending = self.CHECK
        clear = self.dp_access(True, dpmod.Dp.RDBUFF, 0)
        self.__check = (stat, clear)

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

            slot = [result, None,
                    TypeError(f"Unhandled DP op: {type(op).__name__}")]
            self.__slots.append(slot)
        if self.ctrl_stat is not None:
            self.sticky_check()
        else:
            self.flush()
        self.__lowering = False
        if not self.__unsettled:
            self.__finalize()

# --- DP overlay ----------------------------------------------------

class JtagDp(dpmod.Dp):
    """ARM Debug Port over JTAG. Translates batched DP/AP ops to
    DPACC/APACC shifts on the parent :class:`JtagDpTap`."""

    def __init__(self, name: str = "dap", jtag_protocol_version: int = 0):
        super().__init__(name)
        self.__select: int | None = None  # cached SELECT value
        if jtag_protocol_version not in (0, 1):
            raise ValueError(
                f"JTAG-DP protocol version must be 0 or 1, "
                f"got {jtag_protocol_version!r}")
        self.__jtag_protocol_version = jtag_protocol_version
        self.__ctrl_stat: int | None = None

    async def flush_ops(self, batch):
        """Lower a DP/AP batch to JTAG-DP wire shifts."""

        try:
            lowerer = JtagDpLowerer(self.__jtag_protocol_version,
                                    self.parent, self.__ctrl_stat)
            lowerer.process(batch)
            self.__ctrl_stat = lowerer.ctrl_stat
        except Exception as e:
            import traceback
            traceback.print_exc()
