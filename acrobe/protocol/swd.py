"""ARM Serial Wire Debug (SWD) protocol layer.

Defines the bit-level SWD operations that an :class:`Interface`
backend executes on the wire (Read/Write/Run/Wakeup plus the
mode-switch sequences), and the abstract :class:`Interface` class
itself. Adapter-specific subclasses (J-Link, CMSIS-DAP, FTDI-SWD,
…) register against ``Interface.db`` and implement
:meth:`flush_ops` to drive their own hardware.

The DP layer (:class:`acrobe.component.arm.SwDp`) sits above this:
it lowers DP/AP register accesses (``DpRead``/``ApRead``/…) into
the swd ops below and posts them on its parent ``Interface``.
SELECT-cache management lives there; AP-read pipelining (the
"data lands on the next packet" SWD wire quirk) is the
``Interface`` implementation's responsibility — callers post a
:class:`Read` with ``ap=True`` and the future resolves with the
real data once the implementation has flushed the trailing read."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .. import wire
from ..db import Db
from ..engine import Batcher
from ..freq_capper import FreqCapper
from ..node import Node


class Ack(IntEnum):
    """SWD ACK encoding (ACK[0] is the first wire bit)."""

    OK    = 0b001
    WAIT  = 0b010
    FAULT = 0b100


# --- Operation dataclasses --------------------------------------------
#
# Inputs only — the future returned by Batcher.post resolves to the
# natural result value (int for Read, None otherwise).


@wire.op("5586fd6c-2331-4baa-b4c6-cb64b6ebd014")
@dataclass(frozen=True, slots=True)
class Read:
    """Read a DP or AP register.

    ``addr`` is the byte offset; only A[3:2] (bits 2..3) make it onto
    the wire — the bank field of ``addr`` is the Dp's responsibility
    via SELECT.

    AP reads have an inherent one-packet pipeline delay on the wire.
    The Interface backend handles that internally; the future resolves
    to the actual 32-bit data once the trailing read has been issued."""

    ap: bool
    addr: int


@wire.op("b2dee1b7-e97e-40cf-8c2a-3cd47e90c26e")
@dataclass(frozen=True, slots=True)
class Write:
    """Write a DP or AP register. Future resolves to ``None``."""

    ap: bool
    addr: int
    data: int


@wire.op("436acd28-e821-4fbc-9f78-63c79481e09c")
@dataclass(frozen=True, slots=True)
class Run:
    """``cycles`` idle cycles with SWDIO held LOW.

    Used to insert delays between AP transactions — most chips need
    a handful of idles between back-to-back AP accesses or they
    return WAIT/FAULT."""

    cycles: int


@wire.op("95e6b9b2-d19a-4f2d-b598-37b6ac73ce7d")
@dataclass(frozen=True, slots=True)
class Wakeup:
    """``cycles`` cycles with SWDIO held HIGH (a partial line reset).

    Standalone primitive; :class:`LineReset` and :class:`JtagToSwd`
    bundle it with idles for the full wire-mode setup."""

    cycles: int = 50


@wire.op("badc53e2-f662-4f87-8d75-7a30a5d5caf4")
@dataclass(frozen=True, slots=True)
class JtagToSwd:
    """JTAG-to-SWD switch sequence: line reset + 0x79E7 (MSB-first
    on the wire) + line reset + idle. Idempotent on chips already
    in SWD; doubles as the canonical "wake the SWD interface up"
    primitive at adapter init."""


@wire.op("60ab22a0-f913-4de0-94ae-a60de00ad212")
@dataclass(frozen=True, slots=True)
class LineReset:
    """SWD line reset: ≥50 cycles SWDIO=1 followed by ≥2 idle
    cycles. Resets the DP's SWD state machine and clears its
    SELECT register."""


@wire.op("97c68d05-aa37-44d3-b1e5-b8260a2c9b75")
@dataclass(frozen=True, slots=True)
class SwdToDormant:
    """SWD-to-Dormant: ≥50 SWDIO=1 cycles followed by the 16-bit
    sequence 0xE3BC (LSB-first on the wire). Brings any DPv2+ DP
    from SWD into the dormant state, the canonical multidrop
    pre-condition."""


@wire.op("896eb710-9651-4db2-9fd2-64ea914a11ce")
@dataclass(frozen=True, slots=True)
class DormantToSwd:
    """Dormant-to-SWD: ≥8 SWDIO=1 cycles, the 128-bit selection
    alert, 4 cycles of 0, then the 8-bit SWD activation code
    0x1A. Brings a dormant DP into SWD mode and clears any prior
    TARGETSEL selection on every DP listening to the wire."""


@wire.op("b163e24c-944a-4d3f-8323-5bfd2a326881")
@dataclass(frozen=True, slots=True)
class TargetSelWrite:
    """Write to the multidrop TARGETSEL register (DP addr 0x0c).
    Unlike a normal Write, the ADIv5/v6 spec guarantees that no
    DP responds with OK on this transaction; concrete
    ``flush_ops`` impls MUST accept whatever ACK comes back (or
    none) without raising."""

    target: int


@wire.op("33ba4e4f-8fc0-4866-a065-18755e260e0c")
@dataclass(frozen=True, slots=True)
class TargetSelect:
    """Declare which TARGETID owns the next wire transactions on
    a multidrop bus.

    Posted by :class:`acrobe.component.arm.sw_dp.SwDp`'s
    ``flush_ops`` at the head of each batch when the DP carries a
    non-None ``targetsel``. The Interface's batch pre-pass
    deduplicates against ``current_target`` and only materialises
    the wake / :class:`TargetSelWrite` / idle preamble when the
    wire's selection actually changes.

    Never reaches a concrete adapter's :meth:`flush_ops`: the
    base :class:`Interface` consumes it and either drops it
    silently or lowers it to wire ops."""

    target: int


# --- Errors -----------------------------------------------------------


@wire.error("bd47eb9a-4760-4ead-8a02-0074851310c2")
@dataclass
class SwdAccessFailure(Exception):
    """SWD transaction failed (FAULT, parity error, invalid ACK, …)."""

    detail: str = ""

    def __post_init__(self):
        super().__init__(self.detail)


@wire.error("fbf9342f-2c8a-430d-b298-7bec41f3275d")
@dataclass
class SwdWait(SwdAccessFailure):
    """SWD ACK = WAIT. Caller should clear sticky bits via ABORT and
    retry, or insert idle cycles and retry."""


# --- Abstract Interface ------------------------------------------------


@wire.node("7dc3ddc3-71ce-4dbf-9b83-6cded77a9b73",
           uses=[Read, Write, Run, Wakeup, JtagToSwd, LineReset,
                 SwdToDormant, DormantToSwd, TargetSelWrite,
                 TargetSelect,
                 SwdAccessFailure, SwdWait])
class Interface(Batcher, FreqCapper, Node):
    """SWD wire interface.

    Concrete subclasses (e.g. :class:`acrobe.adapter.jlink.swd.JLinkSwdInterface`,
    :class:`acrobe.adapter.ftdi.swd.SwdMpsse`) implement raw bit-bang
    SWD: :meth:`flush_ops` translates batched swd ops into wire packets
    one-for-one. Adapters whose firmware hardens an ADI command set
    (CMSIS-DAP's DAP_Transfer, ST-Link) sit *above* this layer — they
    register :class:`acrobe.component.arm.dp.Dp` subclasses directly
    and never touch the swd.Interface abstraction.

    Subclasses that need adapter-side setup (mode select, default
    clock, …) override :meth:`start` and call ``await super().start()``
    at the end so the base wire init runs once the adapter is ready.

    Mixes in :class:`FreqCapper` so the wire frequency is always the
    minimum of named constraints (hardware ceiling, user ``fmax``,
    transient enumeration caps, …); subclasses override
    :meth:`FreqCapper.freq_update` to apply the resulting freq to
    hardware.

    :meth:`start` brings the SWD wire up — line reset, JTAG-to-SWD
    switch, DPIDR read — then dispatches through :data:`db` (keyed on
    DPIDR with the upper REVISION nibble masked) to spawn a typed
    :class:`acrobe.component.arm.dp.Dp` subclass attached as the ``"dp"``
    child. The canonical SWD path is therefore ``adapter/swd``, with
    the DP reached as ``adapter/swd/dp``.

    Bare SWD interface use (no DP) is intentionally unsupported: a
    failure to identify a DP raises out of :meth:`start`."""

    # Keyed on raw 32-bit DPIDR; REVISION (bits 31:28) is masked so
    # one registration covers every silicon roll of a DP design.
    @staticmethod
    def __dpidr_eq(key, lookup):
        return (key & 0x0fffffff) == (lookup & 0x0fffffff)

    db: Db = Db("SWD DP DPIDR", eq_func=__dpidr_eq)

    # Catalogue of TARGETIDs that scan mode (`swd(multidrop=scan)`)
    # walks. Vendor modules populate this at import time;
    # each entry value is a short human name used as a child suffix
    # and in logs.
    targetsel_db: Db = Db("SWD multidrop TARGETSEL")

    def __init__(self, name="swd"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        FreqCapper.__init__(self)
        # Multidrop options, set via `option_set` before `start()`.
        self.__targetsel: int | None = None
        self.__multidrop_scan: bool = False
        # Wire-level TARGETSEL selection. None means "unknown /
        # invalidated"; any TargetSelect lowering must re-arm.
        self.current_target: int | None = None

    def option_set(self, key, value):
        if key == "targetsel":
            if self.__multidrop_scan:
                raise ValueError(
                    "swd: targetsel= and multidrop=scan are mutually "
                    "exclusive")
            self.__targetsel = int(value, 0)
            return
        if key == "multidrop":
            if value != "scan":
                raise ValueError(
                    f"swd: unsupported multidrop={value!r}; "
                    "only 'scan' is accepted")
            if self.__targetsel is not None:
                raise ValueError(
                    "swd: targetsel= and multidrop=scan are mutually "
                    "exclusive")
            self.__multidrop_scan = True
            return
        super().option_set(key, value)

    async def flush_ops(self, batch):
        """Translate inter-layer ops (TargetSelect) into wire ops,
        track ``current_target``, then delegate to
        :meth:`flush_wire_ops`.

        Concrete adapter subclasses implement :meth:`flush_wire_ops`,
        not :meth:`flush_ops` — they never see :class:`TargetSelect`
        on their input batch."""
        import asyncio
        loop = asyncio.get_running_loop()
        out: list = []
        for op, future in batch:
            if isinstance(op, TargetSelect):
                if self.current_target == op.target:
                    if future is not None:
                        future.set_result(None)
                    continue
                # Emit a wake / TARGETSEL / DPIDR preamble. We give
                # injected ops their own futures so the wire layer
                # can resolve them normally; nobody awaits them
                # outside this batch. The trailing DPIDR read is
                # spec-mandated: per ADIv5/v6 the first transaction
                # after a Target Selection Protocol must be a DPIDR
                # read to confirm the new selection; without it,
                # follow-up transactions go unacknowledged.
                out.append((Wakeup(50), loop.create_future()))
                out.append((Run(4), loop.create_future()))
                out.append(
                    (TargetSelWrite(op.target), loop.create_future()))
                out.append((Run(4), loop.create_future()))
                out.append((Read(ap=False, addr=0x00),
                            loop.create_future()))
                self.current_target = op.target
                if future is not None:
                    future.set_result(None)
                continue
            # Wire-mode transitions clear every DP's TARGETSEL state.
            if isinstance(op, (SwdToDormant, DormantToSwd,
                               JtagToSwd, LineReset)):
                self.current_target = None
            out.append((op, future))
        await self.flush_wire_ops(out)

    async def flush_wire_ops(self, batch):
        """Lower a :class:`TargetSelect`-free batch to the wire.
        Concrete subclasses implement this — they may assume the
        batch contains only the wire-emitting ops (Read, Write,
        Run, Wakeup, LineReset, JtagToSwd, SwdToDormant,
        DormantToSwd, TargetSelWrite)."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement flush_wire_ops")

    async def start(self):
        """Wake the wire and parent the typed DP(s).

        Three bring-up paths:

        * Default: line reset + JtagToSwd + DPIDR read; spawn a single
          ``dp`` child.
        * ``targetsel=<id>``: dormant cycle + TARGETSEL(id) + DPIDR;
          spawn one DP carrying that ``targetsel``.
        * ``multidrop=scan``: iterate every entry in
          :data:`targetsel_db`, probing one DP per TARGETID; spawn one
          DP per responsive target.
        """
        # Lazy import: protocol layer must not pull in component-tree
        # modules at load time. The acrobe.component.arm package is
        # imported by every adapter that wants SWD, so by the time we
        # get here registrations have fired.
        from ..component.arm.dp import Dp  # noqa: F401

        if self.__targetsel is not None:
            await self.__start_multidrop_single(self.__targetsel)
        elif self.__multidrop_scan:
            await self.__start_multidrop_scan()
        else:
            await self.__start_single_dp()

    async def __start_single_dp(self):
        self.post(LineReset())
        self.post(JtagToSwd())
        self.post(LineReset())
        self.post(Run(cycles=8))
        dpidr = await self.post(Read(ap=False, addr=0x00))
        self.logger.info("DPIDR 0x%08x", dpidr)
        dp = await self.db.acall(dpidr, self, dpidr=dpidr)
        self.child_add(dp)

    async def __start_multidrop_single(self, targetsel: int):
        with self.freq_capped("enumeration", 1e6):
            self.post(Wakeup(50))
            self.post(SwdToDormant())
            self.post(Wakeup(50))
            self.post(DormantToSwd())
            self.post(Wakeup(50))
            self.post(Run(4))
            self.post(TargetSelWrite(targetsel))
            self.post(Run(4))
            dpidr = await self.post(Read(ap=False, addr=0x00))
        # The bring-up just put the wire in a known selected state;
        # record it so the DP's first TargetSelect elides correctly.
        self.current_target = targetsel
        self.logger.info(
            "DPIDR 0x%08x (TARGETSEL 0x%08x)", dpidr, targetsel)
        if dpidr in (0, 0xffffffff):
            raise SwdAccessFailure(
                f"no DPIDR response on TARGETSEL 0x{targetsel:08x}")
        dp = await self.db.acall(
            dpidr, self, dpidr=dpidr, targetsel=targetsel)
        self.child_add(dp)

    async def __start_multidrop_scan(self):
        """Walk :data:`targetsel_db` and spawn a DP per responsive
        target. Each candidate gets a full dormant cycle so the
        previous TARGETSEL is cleared on every DP."""
        entries = [
            (int(key), handlers[0])
            for key, handlers in self.targetsel_db.registry.items()
        ]
        if not entries:
            self.logger.warning(
                "multidrop=scan: targetsel_db is empty; no targets "
                "to probe (no vendor module registered any TARGETID).")
            return
        responsive: list[tuple[int, str, int]] = []
        with self.freq_capped("enumeration", 1e6):
            for targetsel_int, name in entries:
                self.post(Wakeup(50))
                self.post(SwdToDormant())
                self.post(Wakeup(50))
                self.post(DormantToSwd())
                self.post(Wakeup(50))
                self.post(Run(4))
                self.post(TargetSelWrite(targetsel_int))
                self.post(Run(4))
                try:
                    dpidr = await self.post(Read(ap=False, addr=0x00))
                except SwdAccessFailure as exc:
                    self.logger.debug(
                        "TARGETSEL 0x%08x (%s): %s",
                        targetsel_int, name, exc)
                    continue
                if dpidr in (0, 0xffffffff):
                    self.logger.debug(
                        "TARGETSEL 0x%08x (%s): no DPIDR response",
                        targetsel_int, name)
                    continue
                self.logger.note(
                    "TARGETSEL 0x%08x (%s) -> DPIDR 0x%08x",
                    targetsel_int, name, dpidr)
                responsive.append((targetsel_int, name, dpidr))
        # current_target was clobbered by the last dormant cycle in
        # the loop body — leave it as None so each newly-spawned DP's
        # first TargetSelect re-arms.
        self.current_target = None
        if not responsive:
            self.logger.warning(
                "multidrop=scan: no DP responded to any registered "
                "TARGETID.")
            return
        for targetsel_int, name, dpidr in responsive:
            dp = await self.db.acall(
                dpidr, self, dpidr=dpidr, targetsel=targetsel_int,
                name=f"dp-{name}")
            self.child_add(dp)
