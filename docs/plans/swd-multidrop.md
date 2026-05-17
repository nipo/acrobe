# PLAN — SWD multidrop

Living document. Tracks the design and slicing for SWD multidrop
support in acrobe. The synchronous sibling project (crobe) already
ships this; this plan is the asyncio-shaped port, with one
deliberate divergence: multidrop is selected through
`swd(...)` options, not through a separate sub-interface.

References:

* `crobe/protocol/swd.py` — `multidrop_enumerate`,
  `multidrop_probe`, `targetsel_db`, `multidrop_db`.
* `crobe/component/arm/sw_dp.py` — `MultidropSwDp`,
  `TargetSel`-prefixed `execute`.
* `crobe/component/raspberrypi/rp2/swd_target.py` — concrete
  RP2040 / RP2350 registrations.

## Background

DPv2+ adds a TARGETSEL register on the SWD wire. Multiple Debug
Ports share one wire; each DP carries a `TARGETID` (the
manufacturer-defined value that DPv2 puts on TARGETID, ADIv6
puts on DLPIDR/TARGETID, …). A "TARGETSEL write" is a special
write that *every* DP receives but only the addressed DP
acknowledges with OK — the others go silent. After a TARGETSEL
write addressing DP `X`, all subsequent reads/writes on the
wire are answered by DP `X` alone, until the next TARGETSEL
write.

Implications:

* The TARGETSEL write itself does not produce an OK ACK from any
  DP that doesn't match (it asserts no ACK at all, per spec).
  The `flush_ops` lowering must therefore allow at least one
  Write op whose ACK is "no driver on the line" without raising.
* The wire wake-up sequence on a multidrop bus must use the
  SWD-to-Dormant + Dormant-to-SWD pair (the only sequence DPv2+
  is guaranteed to recognise) rather than the JtagToSwd switch
  (which is DPv0/v1 vintage and is silently ignored by some
  DPv2 DPs).
* Even with a single known TARGETID, the bring-up still has to
  prefix the DPIDR read with a TARGETSEL write — otherwise
  every DP on the wire answers and the line goes garbage.

The model splits cleanly into two cases:

1. **Known TARGETID** (`swd(targetsel=<id>)`): we know exactly
   which DP we want. Bring-up is dormant-wake + TARGETSEL(id) +
   DPIDR read. One `dp` child, a `SwDp` instance constructed
   with that `targetsel`.
2. **Scan** (`swd(multidrop=scan)`): we don't know what's on the
   wire. Iterate the registered `targetsel_db` entries; for each,
   do a full dormant-wake + TARGETSEL(id) + DPIDR read; entries
   that respond with a valid DPIDR get a `dp-<short>` child
   spawned (still a `SwDp` or vendor subclass, keyed off the
   DPIDR via the existing `db` and constructed with the matching
   `targetsel`).

Mode A is cheap (one wake-up). Mode B is expensive (one full
dormant transition per registered TARGETID); that's why the
existing design note kept it off the default path.

## Decisions

### D1. Option-driven, not a sub-interface

Per the prior conversation: multidrop is fundamentally a
*different bring-up*, not a layer added after a normal bring-up
succeeded. Expressed via options on the SWD interface, applied
before `start()`:

* `swd(targetsel=<hex>)` — single known target. Bring-up uses
  dormant-wake + TARGETSEL + DPIDR.
* `swd(multidrop=scan)` — enumerate every registered
  `targetsel_db` entry. Yields zero or more DP children, one per
  responsive target.
* No option / `multidrop=false` (default) — current behaviour
  (JtagToSwd + DPIDR). Unchanged.

`targetsel=` and `multidrop=scan` are mutually exclusive
(`option_set` enforces this).

The value of `targetsel=` is parsed as an integer (`int(value, 0)`
to accept `0x...`). For ergonomic CLI use, the targetsel_db keys
may grow a string-alias lookup later (e.g. `targetsel=RP2040-core0`);
out of scope for v1.

### D2. Two new mode-switch ops, one new wire bring-up routine

Add to `acrobe/protocol/swd.py`:

```python
@wire.op("...")
@dataclass(frozen=True, slots=True)
class SwdToDormant:
    """SWD-to-Dormant: ≥50 SWDIO=1 cycles followed by the
    16-bit 0xE3BC pattern (MSB-first). Brings any DPv2+ DP from
    SWD into the dormant state."""

@wire.op("...")
@dataclass(frozen=True, slots=True)
class DormantToSwd:
    """Dormant-to-SWD: ≥8 SWDIO=1 cycles, the 128-bit selection
    alert sequence, 4 cycles of 0, then the 8-bit SWD activation
    code 0x1A. Brings a dormant DP into SWD mode."""
```

(Bit patterns from the ARM ADIv5/v6 spec — same as crobe's.)

Concrete `flush_ops` impls (`FtdiSwd`, `JLinkSwdInterface`,
`CmsisDapSwdInterface`) gain two more entries in their SWJ
slots; on adapters where the firmware command set has no
direct "bit-bang" entry point, the bit pattern is appended to
the existing `_flush_swj_group`. This is purely additive and
each concrete impl is ~10 lines.

ST-Link's `dp.py` (which never sees `swd.Interface` ops and
goes directly to firmware-level DP/AP) does **not** need
SwdToDormant/DormantToSwd — its firmware does not expose a
multidrop primitive at all. ST-Link multidrop support is
**out of scope for v1**; if requested, the adapter will fall
back to JtagToSwd-style bring-up and raise on any `targetsel=`
/ `multidrop=scan` option (logged with a clear message
pointing at the limitation).

### D3. One new registry on `swd.Interface`

```python
class Interface(Batcher, FreqCapper, Node):
    db = Db("SWD DP DPIDR", eq_func=_dpidr_eq)         # unchanged
    # NEW:
    targetsel_db: Db = Db("SWD multidrop TARGETSEL")
```

* `targetsel_db` keys on the **32-bit TARGETID value**
  (`PartId.from_idcode(...)` is convenient at registration sites
  but the registry stores raw int keys, matching how TARGETSEL
  appears on the wire). Values are short human names used in
  logs and as the spawned child's name suffix
  (`dp-rp2040-core0` etc.). Used only by *scan* mode to know
  which TARGETIDs to probe.
* The existing `db` (DPIDR → DP subclass factory) is **reused**
  for both plain and multidrop bring-up — there is no separate
  multidrop registry. A vendor whose silicon needs a specialised
  DP subclass (e.g. RP2040 Rescue DP) registers it once on `db`
  and it works in either mode.

The `db` factory contract gains a new optional kwarg:
`targetsel: int | None = None`. Default-`SwDp` accepts it; vendor
subclasses inherit through `__init__`.

### D4. One `SwDp`, multidrop is a constructor flag

`SwDp` carries an optional `targetsel: int | None = None`. When
set, the DP is on a multidrop wire and must announce itself
before every wire-level transaction. When `None`, behaviour is
unchanged.

```python
@swd.Interface.db.register_default
class SwDp(dpmod.Dp):
    def __init__(self, swd_interface, *,
                 dpidr: int | None = None,
                 targetsel: int | None = None,
                 name: str | None = None):
        super().__init__(name=name or self._default_name(targetsel),
                         dpidr=dpidr)
        self._swd = swd_interface
        self.targetsel = targetsel
        self._select = 0

    def _default_name(self, targetsel):
        return "dp" if targetsel is None else f"dp-{targetsel:08x}"

    async def flush_ops(self, batch):
        if self.targetsel is not None:
            self._swd.post(swd.TargetSelect(self.targetsel))
        # ... existing body unchanged ...
```

The per-batch `TargetSelect` is a single extra post — no
`_post` indirection, no `targeted_post` side-channel on the
Interface, no fork in the lowering path. The DP simply tells the
Interface "I'm about to talk; this is my TARGETID"; the Interface
decides whether the wire actually needs a re-select.

`TargetSelect` is a new op on the Interface's batch surface:

```python
@wire.op("...")
@dataclass(frozen=True, slots=True)
class TargetSelect:
    """Announce that subsequent ops in this batch belong to the
    DP with this TARGETID. The Interface elides redundant
    selects (same target as current) and otherwise emits a wake/
    TARGETSEL-write/run preamble before the next ops on the wire.

    A no-op on non-multidrop wires (Interface.flush_ops simply
    drops it when its `current_target` is already `None` and the
    incoming target is `None`). Posted by `SwDp.flush_ops` when
    its `targetsel` is set."""
    target: int
```

The dedup logic lives in `Interface.flush_ops`:

* `Interface` gains `current_target: int | None`, initially `None`.
* On `TargetSelect(target)`:
  * if `target == current_target`, drop the op (resolve future
    with `None`, emit no wire bytes);
  * else, emit `Wakeup(50)` + `Run(4)` + `TargetSelWrite(target)`
    + `Run(4)` on the wire, then `current_target = target`.
* On `SwdToDormant`, `DormantToSwd`, `JtagToSwd`, `LineReset`:
  reset `current_target = None`. These wipe wire-side selection
  state, so the next `TargetSelect` must re-arm.

Why this works for the *non-multidrop* SwDp on a multidrop wire:
two siblings can coexist if one has `targetsel=None` and the other
`targetsel=X`. The "None" sibling will never post `TargetSelect`,
so once its sibling has selected `X`, the wire stays selected on
`X`, and the None-sibling's transactions go to `X` — meaningless.
**Constraint:** on a multidrop wire, every active `SwDp` MUST
carry a `targetsel`. The bring-up paths in D5 enforce this; the
constraint is documented but not runtime-checked (no cheap way
to detect "wire is multidrop" after the fact).

### D4a. Frequency policy for re-select

The Wakeup+TARGETSEL preamble inside `Interface.flush_ops` must
run at ≤1 MHz per the ADI spec; the surrounding transactions
typically run faster. Two options:

* **(A)** Cap the entire batch when any TargetSelect fires.
  Simple; wastes wire time on the post-preamble ops.
* **(B)** Split the batch in `flush_ops`: emit the preamble at
  the capped rate, then restore the normal rate for the rest.

Concrete `flush_ops` impls already serialise their batch into
chunks (CMSIS-DAP's `_swj_chunks`, FTDI MPSSE's command-grouping).
Take (A) for v1 — simpler, the cost is one batch worth of latency
during a transition. Revisit if profiling shows it matters.

### D5. Bring-up routine

`Interface.start()` is restructured. Today it's a single linear
sequence of posts ending in `db.acall(dpidr, ...)`. With
multidrop, three paths exist:

```python
async def start(self):
    if self._targetsel is not None:
        await self._start_multidrop_single(self._targetsel)
    elif self._do_multidrop_scan:
        await self._start_multidrop_scan()
    else:
        await self._start_single_dp()
```

`_start_single_dp` is today's body, unchanged.

`_start_multidrop_single(targetsel)`:

1. Cap frequency at 1 MHz for the duration of bring-up
   (`with self.freq_capped("enumeration", 1e6):`).
2. Post: `Wakeup(50)`, `SwdToDormant()`, `Wakeup(50)`,
   `DormantToSwd()`, `Wakeup(50)`, `Run(4)`,
   `TargetSelWrite(targetsel)`, `Run(4)`, `Read(False, DPIDR)`.
3. Set `self.current_target = targetsel` (the bring-up bypassed
   `TargetSelect`'s dedup path and put the wire in a known
   state — record it so subsequent `TargetSelect(target=targetsel)`
   posts from the DP elide correctly).
4. Await DPIDR; raise if 0 or 0xffffffff.
5. `dp = await self.db.acall(dpidr, self, dpidr=dpidr,
   targetsel=targetsel)`.
6. `self._child_attach(dp)`.

`_start_multidrop_scan()`:

1. Cap to 1 MHz throughout.
2. For each `(targetsel, _name)` in `targetsel_db.registry`:
   * Dormant-cycle (`SwdToDormant` + `DormantToSwd` + idles)
     to clear any previous selection.
   * `TargetSelWrite(targetsel)` + `Run(4)` +
     `Read(False, DPIDR)`.
   * Collect responses.
3. After the batch, for every responsive `targetsel`:
   * `dp = await self.db.acall(dpidr, self, dpidr=dpidr,
     targetsel=targetsel)`.
   * `self._child_attach(dp)`. The DP's own `start()` (SELECT-
     cache prep, AP discovery, etc.) runs when `start_tree()`
     reaches it; its first `flush_ops` will post a
     `TargetSelect(targetsel)` which the Interface lowers into a
     fresh wake/select preamble (current_target was clobbered
     by the next iteration's dormant cycle during scan, so
     `current_target` is `None` here — re-arm is correct).
4. If no DP responded, log a warning but don't raise — the
   user might be probing a board with nothing populated.

Per-targetsel bring-up cost is dominated by the dormant
transition (≥186 bits) plus the TARGETSEL+DPIDR transaction
(≤80 bits). At 1 MHz that's ~300 µs per entry; a registry of
20 known TARGETIDs scans in ~6 ms. Acceptable.

### D6. ACK handling on TARGETSEL writes

Per ADI spec, a TARGETSEL write produces *no ACK* from any DP
(the addressed DP latches the value but doesn't acknowledge).
Current `flush_ops` implementations raise `SwdAccessFailure`
when the ACK is not OK. Two options:

* **(A)** Add a `Write` op variant (`TargetSelWrite`) that
  `flush_ops` recognises and explicitly does not raise on.
* **(B)** Recognise the address (`Write(ap=False, addr=0x0c)`)
  inside `flush_ops` and skip the ACK check.

(A) is cleaner — a dedicated op makes the intent explicit and
keeps `Write`'s contract uniform. Take (A); register the new
op as part of `wire.node(...)`'s `uses=`. Concrete `flush_ops`
implementations treat `TargetSelWrite` as "issue write payload,
ignore returned ACK pattern, resolve future with None".

```python
@wire.op("...")
@dataclass(frozen=True, slots=True)
class TargetSelWrite:
    """Write to TARGETSEL on a multidrop wire. Unlike a normal
    Write, the spec guarantees that no DP acknowledges this
    transaction; concrete impls must not raise on a missing
    ACK."""
    target: int
```

`_start_multidrop_*` post `TargetSelWrite(target=...)` rather
than `Write(False, TARGETSEL, ...)`. `MultidropSwDp`'s
preamble does the same.

### D7. Per-target registration sites

A new file per vendor that has multidrop targets, listing the
TARGETIDs that vendor's silicon answers to. Mirrors the
existing pattern. First wave:

* `acrobe/component/raspberrypi/rp2/multidrop.py` — RP2040
  (core0, core1, rescue) and RP2350.
* `acrobe/component/nordic/multidrop.py` — nRF54 family
  exposes a multidrop DP per CPU.

Each file populates `targetsel_db` (the "what to probe in scan
mode" list) and, when a vendor's DP needs custom bring-up,
registers a `SwDp` subclass against the existing `db`:

```python
from ....protocol import swd
from ...arm.sw_dp import SwDp

swd.Interface.targetsel_db.register(
    0x01002927,  # TARGETID = PartId(9, 0x13, 0x1002, 0)
    name="rp2040-core0")
swd.Interface.targetsel_db.register(
    0x11002927,
    name="rp2040-core1")

@swd.Interface.db.register(0x0bc12477)  # RP2040 rescue DPIDR
class Rp2040RescueDp(SwDp):
    """RP2040 Rescue DP. Same wire-level behaviour as a stock
    SwDp; carries an extra start() that pulses CTRL/STAT to
    release the cores from bootloader hold."""
    # constructor inherits *targetsel* from SwDp
    async def start(self):
        await super().start()
        # ... rescue poke ...
```

These files are imported from the parent package's `__init__`
so registration fires at import time. Note the absence of a
`multidrop_db` — the same `db` registration covers both modes.

### D8. CLI surface

No new commands. Existing `-r <path>` parsing already handles
options:

* `-r adapter/swd(targetsel=0x01002927)` — pick this DP.
* `-r adapter/swd(multidrop=scan)` — enumerate; resulting tree
  exposes `adapter/swd/dp-rp2040-core0`,
  `adapter/swd/dp-rp2040-core1`, etc. Subsequent `-r` flags can
  drill into one of them.

`acrobe info enumerate -r adapter/swd(multidrop=scan)` then
shows the full subtree. `acrobe debug -r adapter/swd/dp-...`
proceeds normally; each `SwDp`'s `flush_ops` posts a
`TargetSelect(self.targetsel)` at the head of every batch and
the Interface elides redundant ones.

## Slicing

### Slice 1 — wire-level primitives

* Add `SwdToDormant`, `DormantToSwd`, `TargetSelWrite`,
  `TargetSelect` op dataclasses to `acrobe/protocol/swd.py`.
  Update `wire.node(..., uses=...)`.
* Implement the wire-emitting ones (`SwdToDormant`,
  `DormantToSwd`, `TargetSelWrite`) in each concrete
  `flush_ops`: `acrobe/adapter/ftdi/swd.py`,
  `acrobe/adapter/cmsisdap/swd.py`,
  `acrobe/adapter/jlink/swd.py`. About 30 LoC total. Bit
  patterns are spec-defined and identical across adapters.
* `TargetSelWrite` lowers to the same wire bytes as a normal DP
  write to addr 0x0c but suppresses the ACK check.
* `TargetSelect` is handled **above** the adapter, in
  `acrobe.protocol.swd.Interface` itself (since dedup needs the
  shared `current_target` and the surrounding ops are already
  swd ops): add a thin Interface-side pre-pass over each batch
  that translates `TargetSelect` into either nothing or a
  Wakeup/Run/TargetSelWrite/Run sequence, then hands the
  resulting batch to the concrete adapter's `flush_ops`. This
  keeps every adapter ignorant of multidrop.
* `Interface.current_target: int | None`. Reset to `None` when a
  batch contains `SwdToDormant` / `DormantToSwd` / `JtagToSwd` /
  `LineReset` (after they've been lowered to the adapter).
* `Interface.option_set` recognises `targetsel` and `multidrop`
  (with `super().option_set` deferral for `fmax`, etc.).
* One new registry on `Interface`: `targetsel_db`.

Tests:

* Option parser: `targetsel=0x12345678` parses to int;
  `multidrop=scan` sets the flag; the two together raise.
* `TargetSelWrite` mock-wire test — synthetic `flush_ops`
  confirms the future resolves to `None` even when the simulated
  ACK is "no driver".
* `TargetSelect` dedup: a batch with two `TargetSelect(X)` ops
  emits one preamble; a `TargetSelect(X)` followed by `SwdToDormant`
  followed by `TargetSelect(X)` emits two (the dormant transition
  reset `current_target`).

### Slice 2 — bring-up paths and SwDp constructor

* `SwDp` gains `targetsel: int | None = None` constructor kwarg.
  `SwDp.flush_ops` posts `swd.TargetSelect(self.targetsel)` as
  its first op when `self.targetsel is not None`.
* `SwDp._default_name` produces `dp` (no targetsel) or
  `dp-<targetsel:08x>` (targetsel set).
* Refactor `Interface.start` per D5 (three named branches).
* `_start_multidrop_single` and `_start_multidrop_scan`
  implementations.

Tests:

* Synthetic `Interface` subclass that records posts; assert
  the bring-up sequence for each of the three paths.
* `SwDp(targetsel=X)` with a mock interface: two siblings
  posting interleaved ops produce one TARGETSEL preamble per
  switch; back-to-back ops on the same sibling produce one
  preamble (the first); a dormant cycle between two same-target
  posts re-arms the preamble.

### Slice 3 — RP2040 / RP2350 end-to-end

* `acrobe/component/raspberrypi/rp2/multidrop.py` — TARGETIDs
  for RP2040 cores 0/1, RP2040 rescue DP, RP2350 cores 0/1
  populated into `targetsel_db`.
* A `Rp2040RescueDp(SwDp)` registered against the rescue DP's
  DPIDR via the existing `db.register(...)` — its `start()`
  performs the crobe-vintage CTRL/STAT poke that brings the
  cores out of bootloader hold. Inherits the `targetsel`
  constructor kwarg from `SwDp` unchanged.
* Live validation against a Pico/Pico-2 board:
  - `acrobe info enumerate -r 'adapter/swd(multidrop=scan)'`
    shows three DPs.
  - `acrobe debug -r 'adapter/swd/dp-rp2040-core0' halt`
    halts core 0 only.
  - `acrobe debug -r 'adapter/swd/dp-rp2040-core1' halt`
    halts core 1 only.

### Slice 4 — Nordic nRF54 and refinements

* Nordic registrations.
* CLI sugar: `targetsel=` accepts a registered name as a
  string alias (`targetsel=rp2040-core1`) by looking up the
  `targetsel_db` registry. Option parser route: try
  `int(value, 0)` first, fall back to a `targetsel_db` reverse
  lookup.
* ST-Link multidrop: out of scope unless a concrete chip ships
  in a board we own that uses ST-Link as the probe.

## Open items captured for later

* **Multidrop fault recovery**. If a TARGETSEL write succeeds
  but the following DPIDR comes back 0/0xffffffff (signal
  integrity issue, contention with a busy target), `crobe`
  retries up to four times at the dormant level. Port the
  retry policy when Slice 3 runs into real-world reliability
  issues.
* **TARGETID validation post-bring-up**. After `MultidropSwDp`
  is up, an optional sanity check reads `TARGETID` (DPv2+) and
  compares to `self.targetsel`. Useful telemetry, not load-
  bearing for correctness — defer.
* **Multidrop on JTAG-DP**. The JTAG side has its own
  multi-target mechanism (IcePick + ICEPICK_C). Out of scope
  for this plan — covered in `docs/jtag-refresh.md`.
* **Interaction with `targetsel=` and existing power gates**.
  Some platforms gate the SWD bus behind a power domain that's
  off at reset; bring-up may need a pre-step to power that
  domain on. Plan a pluggable `pre_bringup` hook on
  `Interface` if a real chip surfaces the need.
