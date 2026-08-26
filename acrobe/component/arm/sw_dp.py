"""ARM SW-DP — Debug Port over SWD.

Translates DP/AP operations into :mod:`acrobe.protocol.swd` ops
posted to a parent :class:`swd.Interface`. Owns the SELECT cache
and the AP-read pipeline bookkeeping.

The wire layer is deliberately raw: each ``swd.Read`` / ``swd.Write``
turns into exactly one wire packet whose future resolves with the
chip's response data slot. Per IHI0031G §B4.2:

* AP register reads are posted: the data is returned in the data
  phase of the *next* AP register read, or by a DP RDBUFF read.
* DP non-RDBUFF reads preserve the posted result but their own
  data slot carries the DP register's inline value.
* AP register writes and DP register writes destroy the posted
  result.

:meth:`flush_ops` therefore tracks an in-flight AP read, binds
its user-facing future to the wire-future of the *next* op that
drains it (an AP read or an RDBUFF read), and inserts an explicit
RDBUFF read before any op that would destroy the latch (writes,
ABORT, SELECT changes, end of batch).

Wire bring-up (line reset, JTAG-to-SWD switch, DPIDR read) lives in
:meth:`swd.Interface.start` — by the time this class is instantiated
the DPIDR value is already known and is fed in via the ``dpidr``
constructor kwarg."""

from __future__ import annotations

import asyncio

from . import dp as dpmod
from ...protocol import swd


@swd.Interface.db.register_default
class SwDp(dpmod.Dp):
    """ARM Debug Port over SWD.

    Registered as the default factory on :data:`swd.Interface.db`: any
    DPIDR that doesn't match a vendor-specific subclass produces a plain
    :class:`SwDp`."""

    SELECT_REG = 0x08
    ABORT_REG  = 0x00  # write to DP addr 0 = ABORT (read at 0 = DPIDR)
    RDBUFF_REG = 0x0c

    # Idle clock cycles inserted after each AP transaction. Without
    # them the chip's AP can't keep up and returns WAIT/FAULT on the
    # following access. 32 is conservative; OpenOCD uses 8 by default.
    AP_IDLE_CYCLES = 32

    def __init__(self, swd_interface: swd.Interface, *,
                 dpidr: int | None = None,
                 targetsel: int | None = None,
                 name: str | None = None):
        super().__init__(
            name=name or ("dp" if targetsel is None
                          else f"dp-{targetsel:08x}"),
            dpidr=dpidr)
        self.__swd = swd_interface
        # swd.Interface.start() leaves the DP's SELECT register at 0
        # (line reset clears it); pre-seed accordingly so the first
        # access doesn't emit a redundant SELECT write.
        self.__select: int = 0
        # On a multidrop wire, the DP must announce itself at the
        # head of every batch — the Interface deduplicates so
        # back-to-back batches for the same target don't actually
        # send a TARGETSEL preamble.
        self.targetsel: int | None = targetsel

    def __select_for(self, op) -> int:
        """Compute the SELECT value needed to access ``op``'s register.

        For AP ops, ``op.addr`` is the absolute system address, encoded
        ADIv5-style as ``(apsel << 24) | reg_offset``. APSEL goes in
        SELECT[31:24] and APBANKSEL (upper nibble of the register
        offset) in SELECT[7:4]. For DP ops, only DPBANKSEL (lower
        nibble) changes; APSEL/APBANKSEL stick."""
        cur = self.__select
        if isinstance(op, (dpmod.ApRead, dpmod.ApWrite)):
            apsel = (op.addr >> 24) & 0xff
            apbank = (op.addr >> 4) & 0xf
            return (apsel << 24) | (apbank << 4) | (cur & 0xf)
        return (cur & 0xFFFFFFF0) | ((op.addr >> 4) & 0xf)

    async def flush_ops(self, batch):
        # (user_future, wire_future) pairs — we await all wire futures
        # at the end and propagate results / exceptions.
        records: list[tuple] = []
        select = self.__select
        # User-future of the in-flight AP read whose data is in the
        # chip's posted-read latch. The wire-future of the *next* op
        # that drains the latch (an AP read or an RDBUFF read) is
        # bound to this user-future.
        pending: asyncio.Future | None = None

        def drain_pending():
            """Insert an RDBUFF read to extract the pending AP-read
            result. Must run before any write/abort that would
            destroy the latch, and at end of batch."""
            nonlocal pending
            if pending is None:
                return
            f = self.__swd.post(swd.Read(False, self.RDBUFF_REG))
            records.append((pending, f))
            pending = None

        if self.targetsel is not None:
            # The Interface elides this when current_target already
            # matches, so the cost is one Python-side post per batch
            # in the steady state. When the preamble *does* fire,
            # the line reset inside it clears the DP-side SELECT
            # register — invalidate our cache so the first access
            # in this batch unconditionally re-emits SELECT.
            if self.__swd.current_target != self.targetsel:
                self.__select = 0
                select = 0
            self.__swd.post(swd.TargetSelect(target=self.targetsel))

        for op, future in batch:
            if isinstance(op, dpmod.Run):
                # Pure idle on the wire. The chip-side latch is
                # preserved across idle cycles.
                self.__swd.post(swd.Run(op.cycles))
                future.set_result(None)
                continue

            if isinstance(op, dpmod.Abort):
                # ABORT is a DP write — destroys the AP-read latch.
                drain_pending()
                f = self.__swd.post(swd.Write(False, self.ABORT_REG, op.what))
                records.append((future, f))
                continue

            if not isinstance(op, (dpmod.DpRead, dpmod.DpWrite,
                                   dpmod.ApRead, dpmod.ApWrite)):
                future.set_exception(TypeError(
                    f"SwDp can't lower {type(op).__name__}"))
                continue

            new_select = self.__select_for(op)
            if select != new_select:
                # SELECT change is a DP write — drain first.
                drain_pending()
                self.__swd.post(swd.Write(False, self.SELECT_REG, new_select))
                select = new_select

            wire_addr = op.addr & 0xc

            if isinstance(op, dpmod.DpRead):
                if wire_addr == self.RDBUFF_REG:
                    # Explicit RDBUFF read: drains the latch *and*
                    # returns the latched value to the caller — the
                    # wire-future is bound to both ``pending`` (the
                    # previous in-flight AP read) and the user's own
                    # future.
                    f = self.__swd.post(swd.Read(False, wire_addr))
                    if pending is not None:
                        records.append((pending, f))
                        pending = None
                    records.append((future, f))
                else:
                    # DP non-RDBUFF read: returns the DP register's
                    # own value inline. The AP-read latch is
                    # preserved on the chip side, so ``pending``
                    # stays untouched.
                    f = self.__swd.post(swd.Read(False, wire_addr))
                    records.append((future, f))
            elif isinstance(op, dpmod.DpWrite):
                # DP write destroys the AP-read latch.
                drain_pending()
                f = self.__swd.post(swd.Write(False, wire_addr, op.data))
                records.append((future, f))
            elif isinstance(op, dpmod.ApRead):
                # This AP read's data slot carries the *previous*
                # in-flight AP read's value. Bind the previous
                # pending (if any) to this wire-future.
                f = self.__swd.post(swd.Read(True, wire_addr))
                if pending is not None:
                    records.append((pending, f))
                # This AP read becomes the new pending — its data
                # will land in the next AP read's slot or in an
                # explicit RDBUFF drain.
                pending = future
                self.__swd.post(swd.Run(self.AP_IDLE_CYCLES))
            else:  # ApWrite
                # AP write destroys the AP-read latch.
                drain_pending()
                f = self.__swd.post(swd.Write(True, wire_addr, op.data))
                records.append((future, f))
                self.__swd.post(swd.Run(self.AP_IDLE_CYCLES))

        # End-of-batch: drain any trailing in-flight AP read.
        drain_pending()

        self.__select = select

        # Resolve user futures from wire futures. We gather rather
        # than await individually so that a single failure doesn't
        # strand the rest of the batch.
        results = await asyncio.gather(
            *(rec[1] for rec in records), return_exceptions=True)
        for (user_fut, _wire_fut), result in zip(records, results):
            if user_fut.done():
                continue
            if isinstance(result, BaseException):
                if isinstance(result, swd.SwdAccessFailure):
                    # Translate to the DP-layer exception so callers
                    # catching DpAccessFailure don't have to also
                    # know about the wire layer.
                    user_fut.set_exception(
                        dpmod.DpAccessFailure(str(result)))
                else:
                    user_fut.set_exception(result)
            else:
                user_fut.set_result(result)


