"""ARM SW-DP — Debug Port over SWD.

Translates DP/AP operations into :mod:`acrobe.protocol.swd` ops
posted to a parent :class:`swd.Interface`. Owns the SELECT cache;
the wire-level AP-read pipeline is handled inside the Interface so
``ApRead`` futures resolve to real data without callers needing to
chase the trailing read."""

from __future__ import annotations

import asyncio

from . import dp as dpmod
from ...protocol import swd


class SwDp(dpmod.Dp):
    """ARM Debug Port over SWD."""

    SELECT_REG = 0x08
    ABORT_REG  = 0x00  # write to DP addr 0 = ABORT (read at 0 = DPIDR)

    # Idle clock cycles inserted after each AP transaction. Without
    # them the chip's AP can't keep up and returns WAIT/FAULT on the
    # following access. 32 is conservative; OpenOCD uses 8 by default.
    AP_IDLE_CYCLES = 32

    def __init__(self, swd_interface: swd.Interface, name: str = "dap"):
        super().__init__(name=name)
        self._swd = swd_interface
        # The wakeup sequence we issue in start() leaves the DP's
        # SELECT register at 0; pre-seed accordingly so the first
        # DPIDR read doesn't emit a redundant SELECT write.
        self._select: int = 0

    async def start(self):
        # Canonical SWD line wake-up: line reset → JTAG-to-SWD switch
        # → line reset → idle → DPIDR read. Posted back-to-back so the
        # whole sequence flushes in a single batch — the spec requires
        # the first transaction after the switch to be a DPIDR read,
        # and we don't want anything (e.g. a future Dp.start() change)
        # slipping in between. Dp.start() will read DPIDR again on its
        # own; that's redundant but harmless.
        self._swd.post(swd.LineReset())
        self._swd.post(swd.JtagToSwd())
        self._swd.post(swd.LineReset())
        self._swd.post(swd.Run(cycles=8))
        await self._swd.post(swd.Read(ap=False, addr=dpmod.Dp.DPIDR))
        await super().start()

    def _select_for(self, op) -> int:
        """Compute the SELECT value needed to access ``op``'s register.

        For AP ops, ``op.addr`` is the absolute system address, encoded
        ADIv5-style as ``(apsel << 24) | reg_offset``. APSEL goes in
        SELECT[31:24] and APBANKSEL (upper nibble of the register
        offset) in SELECT[7:4]. For DP ops, only DPBANKSEL (lower
        nibble) changes; APSEL/APBANKSEL stick."""
        cur = self._select
        if isinstance(op, (dpmod.ApRead, dpmod.ApWrite)):
            apsel = (op.addr >> 24) & 0xff
            apbank = (op.addr >> 4) & 0xf
            return (apsel << 24) | (apbank << 4) | (cur & 0xf)
        return (cur & 0xFFFFFFF0) | ((op.addr >> 4) & 0xf)

    async def flush_ops(self, batch):
        # (user_future, swd_future, kind) — we await all swd futures
        # at the end and propagate results/exceptions.
        records: list[tuple] = []
        select = self._select

        for op, future in batch:
            if isinstance(op, dpmod.Run):
                self._swd.post(swd.Run(op.cycles))
                future.set_result(None)
                continue

            if isinstance(op, dpmod.Abort):
                f = self._swd.post(swd.Write(False, self.ABORT_REG, op.what))
                records.append((future, f, "abort"))
                continue

            if not isinstance(op, (dpmod.DpRead, dpmod.DpWrite,
                                   dpmod.ApRead, dpmod.ApWrite)):
                future.set_exception(TypeError(
                    f"SwDp can't lower {type(op).__name__}"))
                continue

            new_select = self._select_for(op)
            if select != new_select:
                self._swd.post(swd.Write(False, self.SELECT_REG, new_select))
                select = new_select

            wire_addr = op.addr & 0xc

            if isinstance(op, dpmod.DpRead):
                f = self._swd.post(swd.Read(False, wire_addr))
                records.append((future, f, "dp_read"))
            elif isinstance(op, dpmod.DpWrite):
                f = self._swd.post(swd.Write(False, wire_addr, op.data))
                records.append((future, f, "dp_write"))
            elif isinstance(op, dpmod.ApRead):
                f = self._swd.post(swd.Read(True, wire_addr))
                records.append((future, f, "ap_read"))
                self._swd.post(swd.Run(self.AP_IDLE_CYCLES))
            else:  # ApWrite
                f = self._swd.post(swd.Write(True, wire_addr, op.data))
                records.append((future, f, "ap_write"))
                self._swd.post(swd.Run(self.AP_IDLE_CYCLES))

        self._select = select

        # Resolve user futures from swd futures. We gather rather than
        # await individually so that a single failure doesn't strand
        # the rest of the batch.
        results = await asyncio.gather(
            *(rec[1] for rec in records), return_exceptions=True)
        for (user_fut, _swd_fut, _kind), result in zip(records, results):
            if user_fut.done():
                continue
            if isinstance(result, BaseException):
                user_fut.set_exception(result)
            else:
                user_fut.set_result(result)


@swd.Interface.db.register("dap")
def _spawn_dap(interface: swd.Interface) -> SwDp:
    """Factory invoked by ``swd.Interface.child_spawn('dap')``."""
    return SwDp(interface)
