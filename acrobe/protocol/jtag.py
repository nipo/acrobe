import inspect
import math
from dataclasses import dataclass, field

from .. import wire
from ..engine import Batcher
from ..node import Node
from ..freq_capper import FreqCapper
from ..bitstring import BitString, BitStringBase
from ..db import Db, NoMatch
from ..part_id import PartId


# JTAG Interface Operations
#
# All op classes are immutable. The future returned by Batcher.post()
# resolves to the operation's natural result value:
#   - Shift(read_tdo=True)  → BitString (captured TDO)
#   - Shift(read_tdo=False) → None
#   - CaptureDr/CaptureIr/Reset/Run/SwdToJtag → None
# The legacy "result lives on op.tdo" pattern was removed when these
# classes became transportable: mutation across the wire is meaningless,
# and futures already carry the value cleanly.


@wire.op("9a3b4125-5192-4298-9cb8-4400ed9735e0")
@dataclass(frozen=True, slots=True)
class Shift:
    """Shift data through TDI/TDO.

    ``post_run`` requests ``post_run`` idle TCKs in Run-Test/Idle
    *immediately after* the data shift, baked into the same MPSSE
    submission. Used by upper layers (e.g. ARM JTAG-DP) to insert
    inter-transaction idle without paying for a separate Run op
    cascading through Tap → Chain → JtagInterface.
    """

    tdi: BitString
    read_tdo: bool = True
    post_run: int = 0

    def __repr__(self):
        tdi = repr(self.tdi) if self.tdi is not None else '-'
        no_tdo = ', notdo' if not self.read_tdo else ''
        post = f", run+{self.post_run}" if self.post_run else ''
        return f"Shift({tdi}{no_tdo}{post})"

@wire.op("d21e382f-f032-41ac-8355-5a48f4cfada7")
@dataclass(frozen=True, slots=True)
class CaptureDr:
    """Transition FSM to Capture-DR."""

    def __repr__(self):
        return f"CaptureDr()"

@wire.op("7e14803d-230a-44eb-b2ce-e9fa2da813f6")
@dataclass(frozen=True, slots=True)
class CaptureIr:
    """Transition FSM to Capture-IR."""

    def __repr__(self):
        return f"CaptureIr()"

@wire.op("7e5f7d58-961d-43fa-9be7-6825633db5f7")
@dataclass(frozen=True, slots=True)
class Reset:
    """TAP reset via TMS. `count` is clamped to a 5-cycle minimum."""

    count: int = 5

    @property
    def tms(self) -> BitString:
        return BitString(-1, max(self.count, 5))

    def __repr__(self):
        return f"Reset({self.count})"

@wire.op("8c576220-b357-4559-a59a-4aa4c7c2a4f5")
@dataclass(frozen=True, slots=True)
class Run:
    """Run TCK cycles in Run-Test/Idle."""

    cycles: int

    def __repr__(self):
        return f"Run({self.cycles})"


# Class-level constant for SwdToJtag — the TMS sequence is fixed.
# Kept off the dataclass fields so it doesn't bloat every wire message.
_SWD_TO_JTAG_TMS = BitString(-1, 50) + BitString(0xe73c, 16) + BitString(-1, 5)


@wire.op("ace0ffc5-715c-4938-8187-13bf021362b0")
@dataclass(frozen=True, slots=True)
class SwdToJtag:
    """SWD-to-JTAG switch sequence."""

    @property
    def tms(self) -> BitString:
        return _SWD_TO_JTAG_TMS

    def __repr__(self):
        return f"SwdToJtag()"

# Internal Tap Operations
# Not @wire-decorated yet — Chain/Tap aren't transportable in v1
# because TapOp envelopes carry Python object references that don't
# serialize cleanly. Revisit when wire transport for Chain/Tap is in
# scope.


@dataclass(frozen=True, slots=True)
class _TapShift:
    ir_value: int | None
    tdi: BitString | None
    read_tdo: bool
    # Idle TCKs to insert in Run-Test/Idle either side of this shift.
    # ``pre_dr_run`` runs before DR selection (still issued even when
    # ``tdi`` is ``None``). ``post_dr_run`` runs after the DR shift,
    # baked into the bit-level Shift's framing so it doesn't cost a
    # separate _TapRun op cascading through the layers — used by
    # ARM JTAG-DP for inter-AP idle.
    pre_dr_run: int = 0
    post_dr_run: int = 0

    def __repr__(self):
        ir = f"{self.ir_value:#x}" if self.ir_value is not None else '-'
        tdi = repr(self.tdi) if self.tdi is not None else '-'
        no_tdo = ', -' if not self.read_tdo else ''
        runs = ""
        if self.pre_dr_run:
            runs += f", pre+{self.pre_dr_run}"
        if self.post_dr_run:
            runs += f", post+{self.post_dr_run}"
        return f"TapShift({ir}, {tdi}{no_tdo}{runs})"

@dataclass(frozen=True, slots=True)
class _TapRun:
    cycles: int

    def __repr__(self):
        return f"TapRun({self.cycles})"

@dataclass(frozen=True, slots=True)
class _TapIrStatus:
    """No fields — purely a marker requesting an IR-status capture."""

    def __repr__(self):
        return f"TapIrStatus()"

# Tap → parent submission

class TapOp:
    """A single tap-level op forwarded by a Tap to its parent.

    The Tap is the source context; the parent (Chain, AjiHost, …) uses
    it to look up per-tap state (geometry, open_id, …) and translates
    the inner op accordingly. The parent resolves the future with the
    op's natural result value (BitString for shifts that read, None
    otherwise).
    """

    def __init__(self, tap, op):
        self.tap = tap
        self.op = op

    def __repr__(self):
        return f"{self.tap.name}.{self.op!r}"


# Instruction Registry

class Dr:
    """Class-level descriptor for a data register."""

    def __init__(self, length=None, type=None):
        self.length = length
        self.type = type

    def _spawn(self, name, tap):
        return TapDr(tap, name, length=self.length, type=self.type)


class Instruction:
    """Class-level descriptor for a JTAG instruction. References a Dr by name."""

    def __init__(self, ir, dr=None):
        self.ir = ir
        self.dr = dr

    def _spawn(self, name, tap):
        dr = None
        if self.dr is not None:
            dr = getattr(tap, self.dr)
        return TapInstruction(tap, name, self.ir, dr)


class TapDr:
    """A data register bound to a specific Tap instance."""

    def __init__(self, tap, name, length=None, type=None):
        self.tap = tap
        self.name = name
        self.length = length
        self.type = type

    def __repr__(self):
        return f"<Dr {self.name} length={self.length}>"


class TapInstruction:
    """A bound instruction. Callable: returns Future resolving to TDO value."""

    def __init__(self, tap, name, ir, dr):
        self.tap = tap
        self.name = name
        self.ir = ir
        self.dr = dr

    def __call__(self, tdi=None, read_tdo=None,
                 pre_dr_run: int = 0, post_dr_run: int = 0):
        """Post a DR shift with this instruction. Returns Future -> TDO value.

        ``pre_dr_run`` / ``post_dr_run`` request idle TCKs in
        Run-Test/Idle either side of the DR shift — useful for
        protocols (e.g. ARM AP transactions) that need settling time
        between consecutive accesses, without paying for a separate
        ``Tap.run()`` op cascading through every layer."""
        return self.tap._post_instruction(
            self, tdi, read_tdo,
            pre_dr_run=pre_dr_run, post_dr_run=post_dr_run)

    def __int__(self):
        return int(self.ir) & ((1 << self.tap.irlen) - 1)

    def __repr__(self):
        return f"<Instruction {self.name} ir={int(self):#x}>"


class InstructionRegistry:
    """Mixin: spawns Dr and Instruction class attributes into bound instances."""

    BYPASS_REG = Dr(1)
    BYPASS = Instruction(-1, "BYPASS_REG")

    def _init_instructions(self):
        # Pass 1: spawn Drs
        for name in dir(type(self)):
            obj = inspect.getattr_static(type(self), name)
            if isinstance(obj, Dr):
                setattr(self, name, obj._spawn(name, self))
        # Pass 2: spawn Instructions (they reference spawned Drs)
        for name in dir(type(self)):
            obj = inspect.getattr_static(type(self), name)
            if isinstance(obj, Instruction):
                setattr(self, name, obj._spawn(name, self))

    def instructions(self):
        for v in self.__dict__.values():
            if isinstance(v, TapInstruction):
                yield v


# Dynamic Instruction

class _DynamicInstruction:
    """Callable for ad-hoc IR values not in the InstructionRegistry."""

    def __init__(self, tap, ir_value, dr_length=None):
        self._tap = tap
        self._ir_value = int(ir_value) & ((1 << tap.irlen) - 1)
        self._dr_length = dr_length

    def __call__(self, tdi=None, read_tdo=None):
        if tdi is None:
            if read_tdo is None:
                read_tdo = self._dr_length is not None
            if read_tdo and self._dr_length is not None:
                tdi = BitString(0, self._dr_length)
            elif read_tdo:
                raise ValueError("Cannot determine shift length")
        else:
            if read_tdo is None:
                read_tdo = True
            if isinstance(tdi, int):
                if self._dr_length is not None:
                    tdi = BitString(tdi, self._dr_length)
                else:
                    raise ValueError("Cannot determine shift length from int")
            elif not isinstance(tdi, BitStringBase):
                raise TypeError("tdi must be int, BitString, or None")

        op = _TapShift(self._ir_value, tdi, read_tdo)
        return self._tap.post(op)

    def __repr__(self):
        return f"<DynamicInstruction ir={self._ir_value:#x}>"


# Tap

class Tap(Batcher, Node, InstructionRegistry):
    """A single TAP. Batches TAP-level ops (_TapShift, _TapRun,
    _TapIrStatus) and forwards them to its tree parent (a Chain or any
    other parent-of-taps) wrapped in `TapOp` envelopes.

    The Tap holds no chain geometry and no reference to the underlying
    JTAG interface — its only collaborator is its direct parent. The
    parent owns geometry, current_ir caching, and translation to
    bit-level (or AJI, or whatever else) ops.
    """

    irlen = None
    max_freq = None
    # Db keyed on PartId. Equality is PartId.is_same_part: matches
    # across silicon revisions but requires designer/part to be
    # identical. Both registration and lookup transparently accept
    # raw 32-bit IDCODEs — they're the wire form of a PartId, so we
    # auto-convert via PartId.from_idcode in the eq function.
    @staticmethod
    def _partid_eq(key, lookup):
        if isinstance(key, int):
            key = PartId.from_idcode(key)
        if isinstance(lookup, int):
            lookup = PartId.from_idcode(lookup)
        return key.is_same_part(lookup)

    db = Db("TAP partid", eq_func=_partid_eq)

    def __init__(self, idcode=None, irlen=None, name=None):
        if irlen is not None:
            self.irlen = irlen
        self.idcode = idcode

        if name is None:
            name = f"TAP[0x{int(idcode):08x}]" if isinstance(idcode, int) else "TAP"

        Batcher.__init__(self)
        Node.__init__(self, name)
        self._init_instructions()

    def ir(self, value, dr_length=None):
        """Create a dynamic instruction for an ad-hoc IR value."""
        return _DynamicInstruction(self, value, dr_length)

    def run(self, cycles=1):
        """Post a run operation. Returns Future."""
        return self.post(_TapRun(cycles))

    def ir_status(self):
        """Post an IR status capture. Shifts BYPASS into IR and returns
        the captured IR bits as a BitString.
        """
        return self.post(_TapIrStatus())

    def _post_instruction(self, instr, tdi, read_tdo,
                          pre_dr_run: int = 0, post_dr_run: int = 0):
        """Post a DR shift for a given instruction. Called by TapInstruction.__call__."""
        ir_value = int(instr.ir) & ((1 << self.irlen) - 1)

        if tdi is None:
            if read_tdo is None or read_tdo:
                # Read-only: need length from DR
                if instr.dr and instr.dr.length is not None:
                    tdi = BitString(0, instr.dr.length)
                    read_tdo = True
                elif read_tdo:
                    raise ValueError("Cannot determine shift length for read-only shift")
                else:
                    read_tdo = False
            # tdi stays None -> IR-only shift (only shift IR, no DR)
        else:
            if read_tdo is None:
                read_tdo = True
            if isinstance(tdi, int):
                if instr.dr and instr.dr.length is not None:
                    tdi = BitString(tdi, instr.dr.length)
                else:
                    raise ValueError("Cannot determine shift length from int without DR length")
            elif not isinstance(tdi, BitStringBase):
                raise TypeError("tdi must be int, BitString, or None")

        op = _TapShift(ir_value, tdi, read_tdo,
                       pre_dr_run=pre_dr_run, post_dr_run=post_dr_run)
        return self.post(op)

    async def flush_ops(self, batch):
        """Forward each TAP-level op to the parent wrapped in a TapOp.

        Fire-and-forget with a single batched anchor: every user future
        is paired with its parent future, and one ``add_done_callback``
        on the *last* parent future resolves the whole list at once.
        The parent (Chain) resolves its futures in batch order, so by
        the time the last fires every earlier one is already settled.
        Avoids ``len(batch)`` add_done_callback registrations and
        ``len(batch)`` separate event-loop schedulings on resolution.
        """
        if self._parent is None:
            raise RuntimeError(f"Tap {self.name!r} has no parent to forward to")

        forwarded = []
        for op, future in batch:
            parent_future = self._parent.post(TapOp(self, op))
            forwarded.append((future, parent_future))

        if forwarded:
            forwarded[-1][1].add_done_callback(
                lambda _lf, fwd=forwarded: Tap._resolve_batch(fwd))

    @staticmethod
    def _resolve_batch(forwarded):
        for future, parent_future in forwarded:
            if future.done():
                continue
            try:
                future.set_result(parent_future.result())
            except Exception as exc:
                future.set_exception(exc)

    def __repr__(self):
        return f"<Tap {self._name} irlen={self.irlen}>"


# JTAG Interface

@wire.node("fca89969-2aa1-40f3-9c48-7dacb5117091",
           uses=[Shift, CaptureDr, CaptureIr, Reset, Run, SwdToJtag])
class JtagInterface(Batcher, FreqCapper, Node):
    """Bit-level JTAG master interface.

    Receives bit-level ops (Reset, Run, CaptureDr, CaptureIr, Shift,
    SwdToJtag) via `post`. Concrete subclasses (JtagMpsse,
    JtagBitbang, …) implement `flush_ops` to drive hardware.

    Children are typically a single `Chain`. Chains post bit-level ops
    to the interface; the interface drives them through hardware.
    """
    db = Db("Interface handler")

    def __init__(self, name="jtag"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        FreqCapper.__init__(self)

    async def flush_ops(self, batch):
        raise NotImplementedError

    async def child_spawn(self, name):
        return await self.db.acall(name, self)


# Chain

@wire.error("14b08152-1d47-43a5-b0cd-a3d19783b60f")
@dataclass
class OpenChain(Exception):
    """TDO line is stuck or disconnected."""

    detail: str = ""

    def __post_init__(self):
        super().__init__(self.detail)


class ChainContext:
    """Per-tap state held by Chain.

    Stores the geometry of a TAP within the chain (IR/DR padding) and
    a cached `current_ir` so redundant IR shifts can be elided across
    flushes.
    """

    __slots__ = ("tap", "ir_pre", "ir_post", "dr_pre", "dr_post", "current_ir")

    def __init__(self, tap, ir_pre=0, dr_pre=0, ir_post=0, dr_post=0):
        self.tap = tap
        self.ir_pre = ir_pre
        self.ir_post = ir_post
        self.dr_pre = dr_pre
        self.dr_post = dr_post
        self.current_ir = None

    def __repr__(self):
        return (f"ChainContext({self.tap.name!r}, "
                f"ir_pre={self.ir_pre}, ir_post={self.ir_post}, "
                f"dr_pre={self.dr_pre}, dr_post={self.dr_post})")


class Chain(Batcher, Node):
    """A bit-level JTAG chain. Parent of Taps.

    Receives `TapOp` envelopes from its child Taps via `post`. Looks
    up each tap's geometry in `self._contexts` and translates the
    inner op into bit-level ops (`CaptureIr`, `CaptureDr`, `Shift`,
    `Run`) posted to its own parent — a `JtagInterface`.

    Tracks `current_ir` per tap across flushes so redundant IR shifts
    can be elided. When any IR shift happens, the OTHER taps' IRs are
    loaded with all-1s (BYPASS) by construction; we update their
    cached `current_ir` accordingly.
    """

    def __init__(self, name="chain"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self._contexts = {}  # tap → ChainContext
        self.total_irlen = 0
        self.total_drlen = 0
        # Per-length BitString caches: dr-pad (zeros) and ir-pad (ones).
        # Pre-/post-padding for any given context has constant content
        # but is built fresh for every TapShift; cache it once per
        # length and reuse across the whole chunk.
        self._zeros: dict[int, BitString] = {}
        self._ones: dict[int, BitString] = {}

    def _pad_zeros(self, n: int) -> BitString:
        bs = self._zeros.get(n)
        if bs is None:
            bs = BitString(0, n)
            self._zeros[n] = bs
        return bs

    def _pad_ones(self, n: int) -> BitString:
        bs = self._ones.get(n)
        if bs is None:
            bs = BitString(-1, n)
            self._ones[n] = bs
        return bs

    def context(self, tap) -> ChainContext:
        return self._contexts[tap]

    async def start(self):
        jtag_iface = self.parent_of_class(JtagInterface)
        with jtag_iface.freq_capped("enumeration", 1e6):
            await self._cold_init_and_discover(jtag_iface)

    async def _cold_init_and_discover(self, jtag_iface):
        """Atomic cold-line init + blind discovery.

        Sequence:

        1. TAP reset (≥50 TMS=1 cycles) — load IDCODE/BYPASS in
           every TAP's IR, regardless of prior state.
        2. SWD-to-JTAG switch — flips an SWJ-DP from SWD back to
           JTAG. Harmless on chips without an SWJ-DP.
        3. TAP reset — settles the chain after the switch.
        4. IEEE-1149.7 unlock + 4-wire-mode switch (CCL_LOCK +
           STC2(0,2,1) + STMC(0,0,1)). Capped to 100 kHz; ignored
           by non-cJTAG TAPs because the shift values land in the
           captured IDCODE/BYPASS DR (read-only / 1-bit).
        5. Blind discovery (CaptureDr/IR + length probing).

        Steps 4 and 5 must follow each other without an intervening
        TAP reset — TLR reverts the cJTAG controller to 2-wire mode.
        Discovery in turn requires every TAP to be in IDCODE/BYPASS
        (i.e. *just* after a TAP reset), so the whole chain has to
        be batched into a single uninterrupted post sequence."""
        self._parent.post(Reset(count=50))
        self._parent.post(SwdToJtag())
        self._parent.post(Reset(count=50))
        self._parent.post(Run(1))

        # IEEE-1149.7 escape — flat list of DR-scan widths. For each
        # n: CaptureDr; if n>0, Shift(n bits TDI=-1). Trailing Run(1).
        # Encodes CCL_LOCK=(0,0,1), STC2(0,2,1)=(2,9), STMC(0,0,1)=(0,1).
        # No `read_tdo` — by spec there's nothing observable to capture.
        cjtag_lock_unlock = (0, 0, 1, 2, 9, 0, 1)
        with jtag_iface.freq_capped("cjtag", 1e5):
            for width in cjtag_lock_unlock:
                self._parent.post(CaptureDr())
                if width:
                    self._parent.post(Shift(BitString(-1, width),
                                            read_tdo=False))
            self._parent.post(Run(1))

        await self.discover()

    def children_changed(self):
        try:
            jtag_iface = self.parent_of_class(JtagInterface)
        except LookupError:
            return
        jtag_iface.freq_cap_min(self.children)

    # --- Discovery (bit-level, posts directly to parent) ---

    async def _shift_discover(self, max_length=512, shift_in=None):
        """Probe a register's length by shifting a marker through.

        Shifts a 32-bit marker followed by zeros. The marker's
        position in TDO reveals the register length.
        """
        marker = 0xc05a5a03
        tdi = BitString(marker, 32) + BitString(0, max_length + 4)
        shift = Shift(tdi, read_tdo=True)
        captured = await self._parent.post(shift)
        tdo = captured[:max_length + 32]

        if not int(tdo):
            raise OpenChain("TDO stuck low")

        if tdo == BitString(-1, len(tdo)):
            raise OpenChain("TDO stuck high")

        length = int(math.log2(int(tdo))) + 1
        register = tdo[:length - 32]
        rx_marker = int(tdo[length - 32:length])

        if rx_marker != marker:
            raise OpenChain("TDO changed but marker not received back")

        if shift_in is not None:
            back = BitString(shift_in, len(register))
            shift_back = Shift(back, read_tdo=False)
            await self._parent.post(shift_back)
            await self._parent.post(Run(1))

        return register

    async def discover(self):
        """Blind discovery of the JTAG chain.

        Reliably identifies IDCODEs and TAP count. Determines IR
        lengths from the captured IR pattern (JTAG spec: after
        Capture-IR, each TAP's IR starts with 01 in bits [1:0]).

        Caller MUST have just put every TAP into IDCODE/BYPASS via
        a TAP reset — the first DR shift here assumes that state.
        Issuing a TAP reset from inside this method would defeat the
        cJTAG escape sequence in :meth:`_cold_init_and_discover`,
        so the reset is the caller's responsibility.
        """
        self.logger.trace("Discovering chain...")
        self._parent.post(CaptureDr())
        reset_dr = await self._shift_discover()
        self.logger.trace("DR after reset: %d bits", len(reset_dr))

        self._parent.post(CaptureIr())
        captured_ir = await self._shift_discover(shift_in=-1)
        captured_ir_length = len(captured_ir)
        self.logger.trace("IR captured: %d bits", captured_ir_length)

        self._parent.post(CaptureDr())
        bypass_dr = await self._shift_discover(max_length=captured_ir_length // 2)
        device_count = len(bypass_dr)
        self.logger.trace("BYPASS DR: %d devices", device_count)

        idcodes = []
        pos = 0
        for _ in range(device_count):
            if pos >= len(reset_dr):
                idcodes.append(None)
            elif reset_dr[pos]:
                idcodes.append(int(reset_dr[pos:pos + 32]))
                pos += 32
            else:
                idcodes.append(None)
                pos += 1

        self.logger.note("IDCODEs: %s", ", ".join(
            f"0x{idc:08x}" if idc else "none" for idc in idcodes))

        cutoffs = [i for i in range(captured_ir_length)
                   if int(captured_ir[i:i + 2]) == 1]
        cutoffs.append(captured_ir_length)

        segments = [b - a for a, b in zip(cutoffs, cutoffs[1:])]

        def ir_merge(prefix, parts, count_left):
            if len(parts) < count_left or count_left < 0:
                return []
            if len(parts) == count_left:
                return [prefix + parts]
            result = []
            for i in range(1, len(parts) + 1):
                result += ir_merge(
                    prefix + [sum(parts[:i])], parts[i:], count_left - 1)
            return result

        possibilities = ir_merge([], segments, device_count)

        known_irlens = [self._irlen_for(idc) for idc in idcodes]
        possibilities = [
            p for p in possibilities
            if all(k is None or k == l for k, l in zip(known_irlens, p))]

        if len(possibilities) != 1:
            raise ValueError(
                f"Ambiguous IR lengths: {len(possibilities)} possibilities "
                f"for {device_count} devices (idcodes={idcodes!r}):"
                f" {possibilities!r}, known:"
                f" {known_irlens!r}")

        ir_lengths = possibilities[0]
        self.logger.trace("IR lengths: %s", ir_lengths)

        for idcode, irlen in zip(idcodes, ir_lengths):
            tap = self.tap_add(idcode, irlen)
            self.logger.note("TAP: %s (irlen=%d)", tap._name, irlen)

        await self._parent.post(Run(1))

    @staticmethod
    def _irlen_for(idcode):
        """Look up known IR length for an IDCODE via Tap.db. Returns None if unknown."""
        if idcode is None:
            return None
        try:
            taps = Tap.db.get(PartId.from_idcode(idcode), allow_default=False)
        except NoMatch:
            return None
        irlens = {t.irlen for t in taps if t.irlen is not None}
        if len(irlens) == 1:
            return irlens.pop()
        return None

    # --- Tap registration ---

    def tap_add(self, idcode, irlen, ir_pre=None, dr_pre=None, base=None):
        """Add a TAP to the chain at the current end position.

        If `base` is given, instantiate that class directly. Otherwise
        consult `Tap.db` for an idcode-matching subclass; fall back to
        the generic Tap.
        """
        if ir_pre is None:
            ir_pre = self.total_irlen
        if dr_pre is None:
            dr_pre = self.total_drlen

        if base is not None:
            tap = base(idcode=idcode, irlen=irlen)
        else:
            try:
                tap = Tap.db.call(PartId.from_idcode(idcode) if idcode else None,
                                  idcode=idcode, irlen=irlen)
            except NoMatch:
                tap = Tap(idcode=idcode, irlen=irlen)

        self.total_irlen += irlen
        self.total_drlen += 1

        ir_post = self.total_irlen - ir_pre - irlen
        dr_post = self.total_drlen - dr_pre - 1
        ctx = ChainContext(tap, ir_pre=ir_pre, dr_pre=dr_pre,
                           ir_post=ir_post, dr_post=dr_post)
        self._contexts[tap] = ctx

        # Update existing taps' post values.
        for child_tap, child_ctx in self._contexts.items():
            if child_tap is tap:
                continue
            if child_ctx.ir_pre < ir_pre:
                child_ctx.ir_post += irlen
                child_ctx.dr_post += 1
            else:
                child_ctx.ir_pre += irlen
                child_ctx.dr_pre += 1

        self.child_add(tap)
        return tap

    # --- Bit-level translation of TapOps ---

    async def flush_ops(self, batch):
        """Translate TapOp envelopes from child Taps into bit-level ops
        posted to the JtagInterface parent.

        Pre-/post-padding for the target tap is concatenated into a
        single chain-wide Shift instead of emitting separate
        pre/data/post Shifts. The FSM stays in Shift-DR throughout,
        which halves the TMS framing in MPSSE for multi-tap chains.

        Resolution is batched: each TapOp's resolution is recorded as
        a tuple, and one ``add_done_callback`` on the last bit-level
        future resolves the whole list at once. The parent's batcher
        resolves bit-level futures in batch order, so by the time the
        last fires every earlier anchor is already settled — the
        single callback can synchronously read every op's TDO.
        """
        if self._parent is None:
            raise RuntimeError(f"Chain {self.name!r} has no parent to forward to")

        # Each entry: (top_future, anchor_future_or_None, slice_or_None).
        # anchor_future None: nothing was posted; top_future is already
        # resolved with None. slice: (offset, length) for slicing the
        # anchor's TDO when reading; None means "set None".
        resolutions = []
        last_anchor = None

        for top, top_future in batch:
            if not isinstance(top, TapOp):
                if not top_future.done():
                    top_future.set_exception(
                        TypeError(f"Chain expects TapOp, got {type(top).__name__}"))
                continue

            tap = top.tap
            op = top.op
            ctx = self._contexts.get(tap)
            if ctx is None:
                if not top_future.done():
                    top_future.set_exception(ValueError(
                        f"Tap {tap.name!r} not registered in chain {self.name!r}"))
                continue

            bypass_val = (1 << tap.irlen) - 1

            if isinstance(op, _TapIrStatus):
                self._parent.post(CaptureIr())
                tdi = self._pad_with(self._pad_ones, ctx.ir_pre,
                                     BitString(bypass_val, tap.irlen),
                                     ctx.ir_post)
                shift_future = self._parent.post(Shift(tdi, read_tdo=True))
                self._invalidate_ir_cache_for_shift(tap, bypass_val)
                resolutions.append(
                    (top_future, shift_future, (ctx.ir_pre, tap.irlen)))
                last_anchor = shift_future

            elif isinstance(op, _TapShift):
                ir_done = None
                if op.ir_value is not None and op.ir_value != ctx.current_ir:
                    ir_tdi = self._pad_with(self._pad_ones, ctx.ir_pre,
                                            BitString(op.ir_value, tap.irlen),
                                            ctx.ir_post)
                    self._parent.post(CaptureIr())
                    ir_done = self._parent.post(Shift(ir_tdi, read_tdo=False))
                    self._invalidate_ir_cache_for_shift(tap, op.ir_value)

                # pre_dr_run: idle TCKs in RTI before DR selection.
                # Issued unconditionally so the cycles still happen
                # even when the op carries no DR shift.
                pre_run_future = None
                if op.pre_dr_run:
                    pre_run_future = self._parent.post(Run(op.pre_dr_run))

                if op.tdi is not None:
                    self._parent.post(CaptureDr())
                    dr_tdi = self._pad_with(self._pad_zeros, ctx.dr_pre,
                                            op.tdi, ctx.dr_post)
                    # post_dr_run is baked into the Shift itself so the
                    # adapter can fold the trailing idle into the shift's
                    # MPSSE submission — no separate Run op cascading
                    # through the layers.
                    shift_future = self._parent.post(
                        Shift(dr_tdi,
                              read_tdo=op.read_tdo,
                              post_run=op.post_dr_run))
                    if op.read_tdo:
                        resolutions.append(
                            (top_future, shift_future,
                             (ctx.dr_pre, len(op.tdi))))
                    else:
                        resolutions.append((top_future, shift_future, None))
                    last_anchor = shift_future
                elif op.post_dr_run:
                    # No DR shift but caller still asked for trailing
                    # idle — emit it as a standalone Run.
                    run_future = self._parent.post(Run(op.post_dr_run))
                    resolutions.append((top_future, run_future, None))
                    last_anchor = run_future
                elif pre_run_future is not None:
                    # Only pre_dr_run was emitted; anchor on it.
                    resolutions.append((top_future, pre_run_future, None))
                    last_anchor = pre_run_future
                elif ir_done is not None:
                    resolutions.append((top_future, ir_done, None))
                    last_anchor = ir_done
                else:
                    if not top_future.done():
                        top_future.set_result(None)

            elif isinstance(op, _TapRun):
                run_future = self._parent.post(Run(op.cycles))
                resolutions.append((top_future, run_future, None))
                last_anchor = run_future

            else:
                if not top_future.done():
                    top_future.set_exception(
                        ValueError(f"Unknown tap op: {type(op).__name__}"))

        if last_anchor is not None:
            last_anchor.add_done_callback(
                lambda _lf, r=resolutions: Chain._resolve_batch(r))

    @staticmethod
    def _pad_with(pad_fn, pre: int, data: BitString,
                  post: int) -> BitString:
        """Concatenate ``pre`` bits of padding (from ``pad_fn``), the
        ``data`` BitString, and ``post`` bits of padding. Skips the
        empty concatenations so the common single-tap case (pre==post==0)
        returns ``data`` unchanged."""
        if pre and post:
            return pad_fn(pre) + data + pad_fn(post)
        if pre:
            return pad_fn(pre) + data
        if post:
            return data + pad_fn(post)
        return data

    @staticmethod
    def _resolve_batch(resolutions):
        """Walk the recorded (top_future, anchor_future, slice) tuples
        and resolve each top_future. Called once per batch when the
        last anchor future fires."""
        for top_future, anchor_future, slice_spec in resolutions:
            if top_future.done():
                continue
            try:
                value = anchor_future.result()
            except Exception as exc:
                top_future.set_exception(exc)
                continue
            if slice_spec is None:
                top_future.set_result(None)
            else:
                offset, length = slice_spec
                if offset == 0 and length == len(value):
                    top_future.set_result(value)
                else:
                    top_future.set_result(value[offset:offset + length])

    def _invalidate_ir_cache_for_shift(self, tap, new_ir):
        """An IR shift just happened: this tap loaded `new_ir`, all
        others were padded with all-1s and now hold BYPASS."""
        target_ctx = self._contexts.get(tap)
        if target_ctx is not None:
            target_ctx.current_ir = new_ir
        for other_tap, other_ctx in self._contexts.items():
            if other_tap is tap:
                continue
            other_ctx.current_ir = (1 << other_tap.irlen) - 1

    async def child_spawn(self, name):
        """Delegate to single TAP when chain has exactly one device."""
        if len(self._children) == 1:
            return await self._children[0].child_summon(name)
        raise NoMatch("child", name)

    def __repr__(self):
        return f"<Chain {self._name} taps={len(self._children)}>"


@JtagInterface.db.register("chain")
def _spawn_chain(interface):
    """Factory invoked by JtagInterface.child_spawn('chain'). The
    `interface` arg is the spawning JtagInterface; we don't store it
    because Chain reaches its parent via the Node tree.
    """
    return Chain()
