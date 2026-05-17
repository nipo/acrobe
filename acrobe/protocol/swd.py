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
                 SwdAccessFailure, SwdWait])
class Interface(Batcher, FreqCapper, Node):
    """SWD wire interface.

    Concrete subclasses (e.g. :class:`acrobe.adapter.jlink.swd.JLinkSwdInterface`,
    :class:`acrobe.adapter.cmsisdap.swd.CmsisDapSwdInterface`) implement
    :meth:`flush_ops` to translate batched swd ops into adapter-specific
    USB transactions. Subclasses that need adapter-side setup (mode
    select, default clock, …) override :meth:`start` and call
    ``await super().start()`` at the end so the base wire init runs
    once the adapter is ready.

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

    def __init__(self, name="swd"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        FreqCapper.__init__(self)

    async def flush_ops(self, batch):
        raise NotImplementedError(
            f"{type(self).__name__} must implement flush_ops")

    async def start(self):
        """Wake the wire and parent the typed DP.

        Posts LineReset → JtagToSwd → LineReset → 8 idle cycles
        back-to-back so the spec-mandated "first transaction after
        the switch is a DPIDR read" property holds in a single batch,
        reads DPIDR, then looks the value up in :data:`db` to build
        the right :class:`Dp` subclass."""
        self.post(LineReset())
        self.post(JtagToSwd())
        self.post(LineReset())
        self.post(Run(cycles=8))
        dpidr = await self.post(Read(ap=False, addr=0x00))
        self.logger.info("DPIDR 0x%08x", dpidr)
        # Lazy import: protocol layer must not pull in component-tree
        # modules at load time. The acrobe.component.arm package is
        # imported by every adapter that wants SWD, so by the time we
        # get here registrations have fired.
        from ..component.arm.dp import Dp  # noqa: F401  (forces module load)
        dp = await self.db.acall(dpidr, self, dpidr=dpidr)
        self.child_add(dp)
