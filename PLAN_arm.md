# ARM debug: DP / AP / CoreSight discovery for acrobe

Async port of crobe's ARM debug subsystem, modernized to ADI-6 / ARMv8(M),
keeping the parts of crobe that aged well and rebuilding the parts that
didn't. First-class objective: full component-tree enumeration parity
with crobe (the Zynq-7 dump in the conversation that seeded this plan)
through both JTAG-DP and SWD-DP, with a clean gate node for
power-gated subtrees.

Target framework on top is out of scope for this plan; the existing
`PLAN.md` covers Layer-4. The component tree this plan builds is the
substrate that future Target work will key off.

## Status

`acrobe/component/arm/` already has a first-pass `SwDp`, `Ap`,
`MemAp`, `Cortex` and bus-backed memory regions. They predate current
acrobe conventions and several are incorrect:

* Op classes (`ApRead`, `ApWrite`, `DpRead`, `DpWrite`, `MemRead`,
  `MemWrite`) are mutable and carry their result via `op.data`. The
  codebase moved to **frozen dataclass ops + future-resolves-to-result**
  (see CLAUDE.md and `protocol/jtag.py`); ARM is the last holdout.
* `MemAp.flush_ops` mixes typed result entries and doesn't reliably
  wire `MemRead.data` into the user-facing future before resolving.
* `Cortex` and `SwDp.start` use `for _ in range(100)` busy-loops with
  no `await asyncio.sleep` — they tight-loop the event loop until a
  bit changes.
* No JTAG-DP, no abstract `Dp` base, no AP discovery from DP, no
  CoreSight component classification, no ROM walker.
* `Cortex` builds directly on `MemAp`. SCS isn't modelled as a
  CoreSight component; it can't be discovered via a ROM table.

Everything below is a clean rebuild of this subtree. `puppet.py`
and `target/arm/memory.py`'s `StubFlash` were ported euphorically
from crobe and never tested against the Batcher model — they go
together with the broken ARM layer and will be rewritten when the
new ARM tree is ready to host them. The bus-backed memory regions
in `component/arm/memory.py` (`BusRam`, `BusFlash`) are unaffected
and survive (they only depend on `MemAp.mem_read` / `mem_write`,
which the new MemAp will keep).

## Goals

* **Same enumeration output as crobe** for the Zynq-7 case (and any
  ADI-5 chip). Adapter → JTAG chain → JTAG-DP/SW-DP → DAP → APs →
  ROM tables → typed CoreSight components, with chip-ID and
  vendor-specific knowledge surfaced as Node attributes.
* **ADI-6 / ARMv8(M) ready from day one.** New SELECT layout (with
  `DPBANKSEL` and `APBANKSEL` plus the ADIv6 large-AP-address
  encoding), DPIDRv2 fields, DEVARCH-driven component classification,
  multi-AP DPs beyond 256 APs.
* **Preserve crobe's good shape.** Component layout (`component/arm/{dp,ap,mem_ap,jtag_dp,sw_dp}.py`,
  `component/arm/coresight/*.py`, `cpuid.py`), `Db` placement,
  registration patterns. The pending-read lowering scheme (one of
  the most subtle bits of crobe) ports almost verbatim onto
  `Batcher`.
* **Fix the parts crobe got wrong**: ROM walker that treats power-gated
  entries as a first-class concept (gate Node, not `FailedComponent`
  placeholders), eliminate side-effecting properties, separate
  abstract Dp from wire-specific JTAG-DP/SW-DP cleanly.

Non-goals for this plan:

* Target framework — chip-ID matching, Target/Field/Region driver
  selection. Out of scope; covered by `PLAN.md`.
* Wire-transport of DP/AP/MemAp/CoreSight ops. Not now. The op classes
  will be frozen dataclasses (so they *could* be wire-decorated
  later) but `@wire.op`/`@wire.node` UUIDs are deferred until there's
  a use case.
* SWD multidrop, JTAG-AP, vendor APs (TI, NXP). Stubs allowed; full
  support deferred.
* CPUID feature-dump tool. Carried over but as an opt-in inspector,
  no autodiscovery dependency.

## Architecture

### Layer map

```
JTAG chain (existing)        SWD interface (existing)
        │                              │
        ▼                              ▼
 JtagDp (Tap subclass)            SwDp (Batcher Node)
        │                              │
        └─────────── Dp (abstract) ────┘
                       │
                       ▼
                  Ap registry
                   ├── MemAp (AHB / APB / AXI; BD-bank fast-path)
                   ├── JtagAp  (deferred; stub initially)
                   └── vendor APs (deferred)
                       │
                       ▼ (per MemAp's bus)
              MemoryMappedComponent.cast()
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
   class-0x1 ROM Table          class-0x9 CoreSight
    (recursive walk)             (DEVARCH / dev_type)
        │
        ▼
  PowerGate (passive Node) ── inactive children parented here
```

### DP layer (`component/arm/dp.py`)

* **Abstract `Dp(Batcher, Node)`**: defines the DP register model
  (DPIDR, DPIDR1 for ADIv6, CTRL/STAT, SELECT with `APBANKSEL` /
  `DPBANKSEL` / `APSEL`, SELECT1 for ADIv6 large addresses, RDBUFF,
  ABORT, TARGETID, DLPIDR, EVENTSTAT, DLCR), and the AP/DP op
  contract. Subclasses implement `flush_ops`.
* **Op dataclasses (frozen, inputs-only)**:
  ```python
  @dataclass(frozen=True)
  class ApRead:  ap: int; addr: int           # → Future[int]
  @dataclass(frozen=True)
  class ApWrite: ap: int; addr: int; data: int  # → Future[None]
  @dataclass(frozen=True)
  class DpRead:  addr: int                    # → Future[int]
  @dataclass(frozen=True)
  class DpWrite: addr: int; data: int         # → Future[None]
  @dataclass(frozen=True)
  class Run:     cycles: int                  # → Future[None]
  ```
  No `op.data`, no mutation. `Batcher.post(op)` resolves to the
  natural result value, matching `protocol/jtag.py`.
* **`Dp.start()`**: read DPIDR (and DPIDR1 if ADIv6 advertised),
  power up debug+system domains via CTRL/STAT (`CDBGPWRUPREQ` /
  `CSYSPWRUPREQ`), poll for ACK with **`await asyncio.sleep(...)`**
  between attempts, enable `ORUNDETECT`. Drives AP enumeration
  (`__discover_aps`).
* **AP enumeration**: ADIv6 says "walk APs by index until IDR=0";
  ADIv5 used 256 indices. Implementation reads APs 0..255 (with the
  appropriate `APSEL` field width), instantiating `Ap.cast()` for
  each non-zero IDR; for ADIv6, follow the architected pointer scheme
  from base address. Children are added to the Dp Node.
* **ABORT** is a method (`async dp.abort(what=0x1f)`), not a property.
* **No properties with I/O side effects.** `ctrlstat`, `dlcr` etc.
  become `async read_ctrlstat()` / `async write_ctrlstat(value)`
  methods that return futures.

### Wire-specific DPs

**`JtagDp(Tap)` in `component/arm/jtag_dp.py`**:

* Registered against `Tap.db` with the JTAG-DP IDCODEs (crobe's
  `PartId(4, 0x3b, 0xba00)` / `0xba01` plus the ARMv8 variants
  introduced by ADIv6). Existing acrobe `Chain.tap_add` already
  consults `Tap.db`, so registration alone slots JTAG-DPs into the
  chain Node tree.
* Defines `IR_DPACC=0xa`, `IR_APACC=0xb`, `IR_ABORT=0x8`,
  `IR_IDCODE=0xe` as `Instruction(...)` class attributes; the 35-bit
  DR shift is built from BitString ops on the existing Tap shift
  primitive.
* **Pending-read lowering** (the carryover of crobe's scheme,
  expressed against `Batcher`):
  * Walk the incoming `(op, future)` batch.
  * Maintain `pending: (op, future) | None` — the AP read whose
    data hasn't been retrieved yet.
  * On AP read: emit DPACC AP shift; if a `pending` exists, point
    its future at this shift's TDO.
  * On AP write / DP read / DP write / Run / SELECT change: flush
    `pending` by emitting a DP RDBUFF read whose TDO resolves the
    pending future.
  * End-of-batch: flush pending with a final RDBUFF.
  * SELECT tracking: cached in the Dp; only re-issued when a bank
    or AP index actually changes.
* WAIT-handling: wrap the bit-level shift execution in a retry loop
  with a configurable cap; on WAIT, re-emit the access. Retry on
  ERROR via `ABORT(STKERRCLR)` then re-emit.

**`SwDp(Batcher, Node)` in `component/arm/sw_dp.py`** (later, after
JTAG-DP works):

* Registers against an `swd.Interface.db` factory keyed on DPIDR
  parts (mirrors crobe's `swd.Interface.db.register(*parts)`).
  `protocol/swd.py` already exists; it'll need the `Interface.db` /
  `multidrop_db` registries added.
* Same pending-read lowering, against SWD `Read`/`Write` primitives.
* `register` access mode (synchronous AP reads) supported as an
  init flag; default `raw` (deferred, pipelined).
* Multidrop wrapper (`MultidropSwDp`) deferred to a later milestone
  but the abstraction is shaped to accept it: extra `TargetSel` op
  prepended to each batch.

### AP layer (`component/arm/ap.py`, `component/arm/mem_ap.py`)

* **`Ap(Node)`**: holds `index`, exposes `reg_read(addr)` /
  `reg_write(addr, data)` returning futures via parent Dp. ADIv6
  64-bit AP addresses handled here (SELECT1 manipulation lives at
  Dp level; AP just sees a flat address space).
* **`Ap.cast()`**: read IDR, look up in `Ap.db` (Db with
  IDR-revision-masking eq func, mirrors crobe), instantiate the
  matching subclass with the same DP/index. Replaces the placeholder
  `Ap` in the Node tree.
* **`MemAp(Ap, Bus, Batcher)`** — implements the existing `Bus`
  protocol (memory.py's regions consume it):
  * Frozen op dataclasses for memory ops (`Read8/16/32`,
    `Write8/16/32`, `ReadBlock`, `WriteBlock`).
  * CSW bitfield model (size, addrinc, prot, dbgsw_en); cached and
    only rewritten when a transfer requires a different CSW.
  * TAR cached and only rewritten across address-block boundaries
    or when auto-increment wraps the AP's wrap window
    (`wrap_mask`, default 0x3ff = 1KB; configurable, used by APs
    with smaller windows).
  * BD0..BD3 fast-path for word accesses within a 16-byte block:
    skips the inner TAR write entirely.
  * Auto-increment for sequential same-size reads/writes; falls
    back to per-transfer TAR when sizes mix or address jumps.
  * Block ops (`ReadBlock`, `WriteBlock`) lower to chunks bounded
    by the wrap window; resolve a single user-future to a
    list/bytes of all chunks.
  * `mem_read(addr, size)` / `mem_write(addr, data)` convenience
    methods on top, peeling unaligned head/tail. Already exist in
    the current code; logic is correct, plumbing onto the new ops
    is the rewrite.
  * `BASE` register read on `start()` to get the ROM base; if the
    base is non-empty, attach a child `RomTable` at that address as
    the entry point for CoreSight discovery.
* **`JtagAp`** in `component/arm/jtag_ap.py`: stub registration only;
  follow-up work to expose its embedded chain.

### CoreSight discovery (`component/arm/coresight/`)

Files: `model.py`, `rom_table.py`, plus one file per concrete
component (`scs.py`, `dwt.py`, `fpb.py`, `itm.py`, `etm.py`,
`tpiu.py`, `etb.py`, `cti.py`, `pmu.py`, `dbg.py`,
`power_gate.py`, ...).

**`MemoryMappedComponent(Node)`** in `coresight/model.py`:

* Reads PIDR0..7 (4-byte components at +0xfd0..0xfec) and CIDR0..3
  (+0xff0..0xffc) on `start()` to obtain `partid`, `revision`,
  `component_class` (CIDR[15:12] of CIDR1).
* If `component_class == 0x9` (CoreSight), reads DEVARCH (+0xfbc),
  DEVID (+0xfc8), DEVTYPE (+0xfcc).
* Three `Db` lookup tiers in a fixed order:
  1. **`MemoryMappedComponent.devarch_db`** keyed on DEVARCH
     `ARCHITECT/ARCHID` (ADIv6 architected components).
  2. **`MemoryMappedComponent.db`** keyed on `PartId` (ADIv5
     and earlier; some ADIv6 chips also identify here).
  3. For component_class == 0x9: **`CoresightComponent.db`** keyed
     on `DEVTYPE` (the legacy 0x11/0x13/0x15/0x21 lane).
  4. For component_class == 0x1: instantiate `RomTable` directly
     (its own `class_db` registration).
* If no Db hits, instantiate the base `MemoryMappedComponent` with
  a friendly name derived from PIDR (JEP106 + part) so enumeration
  output still labels the component.
* Per-SoC overrides registered to **`RomTable.soc_db`** keyed on
  `(PartId, address)` — gives chip-specific drivers for components
  that share a generic ID across SoCs (mirrors crobe).

So: **3 component-classification Dbs** (`devarch_db`, `db`,
`CoresightComponent.db`) + **1 SoC-override Db** + the implicit
class-0x1 → RomTable mapping. Matches the crobe split exactly,
just without crobe's `class_db` indirection (class-0x1 is the only
class that needed it, and we hardcode that branch).

**`RomTable(MemoryMappedComponent)`** in `coresight/rom_table.py`:

* On `start()`: walk entries 0x000..0xefc (legacy 32-bit ROM) or
  0x000..0xfff in 8-byte strides (ADIv6 64-bit ROM, detected via
  DEVARCH). For each present entry, compute the absolute child
  address from `(self.base & ~0x3ff) + (entry & ~0x3ff)`.
* For each child address, instantiate `MemoryMappedComponent` and
  call `.cast()`. The cast may raise on unreachable memory; this is
  where power gating manifests.
* Child gating: see "Power gating" below. By default, an unreachable
  child becomes a `PowerGate` parent with the failure recorded; if
  later poked (by the Target framework) to enable, it re-runs the
  cast and parents the real component underneath.
* Component-class 0x1 children produce nested `RomTable`s; recursion
  terminates when a ROM table has no entries.

### Power gating (`component/arm/coresight/power_gate.py`)

This is the part of crobe's design that bit hardest. Replacement
model (agreed in conversation):

* Enumeration is **passive** by default. The walker **never** writes
  to enable/wake bits during discovery, never touches AP CSW prot
  fields beyond what's needed to read PIDR/CIDR, never powers a
  domain.
* Power-up of the DP itself is the **only** unconditional enablement
  the walker performs. (Without it, no AP access works at all.)
* When a ROM-table entry is unreachable (read fault, all-zero PIDR,
  AP error response, secure-locked), insert a **`PowerGate(Node)`**
  in its place. The gate carries:
  * The would-be child's address.
  * The diagnostic that made it look unreachable
    (`failure_kind: enum {FAULT, ZERO_ID, SECURE_LOCKED, EMPTY}`).
  * A method `async retry()` that re-attempts cast and, on success,
    parents the discovered component beneath the gate (no Node
    promotion — the gate stays as the parent).
* `PowerGate` is a passive Node — never enables anything itself.
  Target framework, when it lands, walks the tree and may call
  `gate.retry()` after enabling power on the relevant domain via a
  driver-specific path.
* Re-enumeration is *additive*: a successful retry adds children
  beneath the gate. Children are not removed when power drops back;
  the gate may grow a `currently_accessible` flag later.

### Cortex re-shape

`component/arm/cortex.py` rewires:

* SCS is no longer `cortex.py`'s problem. It moves to
  `coresight/scs.py` as a `MemoryMappedComponent` registered against
  the relevant PartIds (Cortex-M3 0x000, M4 0x00c, ARMv8M
  DEVARCH 0x2a04, etc.) plus the `RomTable.soc_db` (PartID 0x470,
  address 0xe000e000) override that crobe carries.
* SCS exposes async methods, never properties: `state()`,
  `halt_cause()`, `halt()`, `resume()`, `step()`, `reset()`,
  `reg_read(reg)`, `reg_write(reg, value)`. Polling loops use
  `await asyncio.sleep(...)` not tight loops.
* `Cortex(Node)` becomes a thin façade that holds an SCS reference
  and exposes the high-level CPU API. It gets attached by the
  Target framework later (it's not a CoreSight component itself);
  not part of the discovery walk.

### CPUID dumper (`component/arm/cpuid.py`)

Port of crobe's `CpuidDumper` and `cpuid` decoder. Pure data + a
small read helper. Used as an inspector tool (CLI subcommand under
`acrobe info ...`); never part of automatic enumeration. Update
register decoders to ARMv8(M) feature-register layouts.

## Slice order

In implementation order (each slice ships independent, tested):

1. **`Dp` abstract + frozen dataclass ops + `JtagDp` + `Tap.db`
   registration.** Dropping the existing `dp.py` mutable-op design
   in favor of frozen dataclasses with future-resolves-to-result.
   Includes pending-read lowering and SELECT caching. Verifiable
   end-to-end against the existing JTAG chain by reading DPIDR on
   the Zynq-7 board (or a synthetic JTAG fixture).
2. **`Ap` base + `Ap.db` + `cast()` + AP enumeration in `Dp.start`.**
   Output: enumerated APs as Node children, each labelled by IDR
   class.
3. **`MemAp` rewrite.** New op dataclasses, CSW/TAR cache,
   auto-increment, BD-bank fast-path, block ops, wrap-window
   handling. Plumb into existing `BusRam`/`BusFlash`. Validate by
   reading and writing memory.
4. **`MemoryMappedComponent` + Dbs.** PIDR/CIDR/DEVARCH parsing;
   three-tier classification; component naming.
5. **`RomTable` walker + `PowerGate` model.** Recursive walk;
   gate nodes for unreachable subtrees.
6. **CoreSight component drivers** sufficient to pretty-name the
   Zynq-7 tree: SCS, DWT, FPB, ITM, ETM, ETB, TPIU, CTI, PMU,
   Dbg. Most are stubs that just claim the right DEVTYPE/PartID
   and provide a name; their actual register models can land
   incrementally.
7. **`SwDp`** + `swd.Interface.db` registry. Re-uses the same
   pending-read scheme as `JtagDp` against SWD primitives.
8. **CPUID dumper** as an inspector subcommand.

After 1–6, `acrobe info enumerate -r .../jtag/chain` reaches parity
with crobe on the Zynq-7 example, end-to-end through the existing
JTAG transport.

## What we keep, drop, rewrite from crobe

| Crobe                                | Disposition                                                                                             |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `component/arm/{dp,ap,mem_ap}.py` shape | **Keep.** Same files, same responsibilities.                                                          |
| `Dp.{ApRead,ApWrite,Run}` op classes | **Rewrite** as frozen dataclasses with no `__op` / `__value_op` mutation; results via futures.           |
| Pending-read lowering scheme         | **Keep.** Logic ports almost verbatim; bookkeeping moves from per-op attributes to local dicts in `flush_ops`. |
| WAIT/ERROR retry loop                | **Keep**, with bounded retries and proper async sleep between attempts.                                 |
| `Ap.db` keyed on IDR, `MemAp` IDR registrations | **Keep** as-is.                                                                              |
| `MemAp` CSW/TAR caching, BD-bank fast-path, addrinc | **Keep**. Logic is sound; rewrite mechanics onto frozen ops.                              |
| `coresight/model.py`'s 3-tier Db lookup | **Keep**, drop the `class_db` indirection (only class-0x1 used it).                                  |
| `RomTable` walker                    | **Rewrite** for power gating (PowerGate Node instead of FailedComponent placeholder) and ADIv6 64-bit entries. |
| `CoresightComponent.db` (DEVTYPE-keyed) | **Keep** for legacy class-0x9 lookup.                                                                |
| Per-component drivers (SCS, DWT, FPB, ITM, ETM, ETB, TPIU, CTI, PMU, Dbg) | **Rewrite** mostly: class structure stays, methods async, no side-effecting properties. |
| `RomTable.soc_db` per-SoC overrides  | **Keep.** Same `(PartId, address)` keying.                                                             |
| `cpuid.py` decoder + `CpuidDumper`   | **Keep**, update for ARMv8(M) feature-register layouts.                                                |
| Side-effecting properties (`scs.cpu_state`, `dp.ctrlstat`, `cortex.halt_cause`, etc.) | **Drop.** Replace with async methods.                                |
| Busy-loop polls without sleep        | **Drop.** Replace with `await asyncio.sleep(...)` between checks; bounded total time.                  |
| `FailedComponent` placeholder        | **Drop.** `PowerGate` replaces it with retry semantics.                                                |
| `JtagAp`                             | Stub registration; full implementation deferred.                                                       |
| Multidrop SWD (`MultidropSwDp`)      | Abstraction shaped to accept it (extra `TargetSel` op); implementation deferred.                       |
| Vendor APs (TI ICEPick, NXP)         | Deferred. Out of scope; clean Db registration path so plugins can add them later.                      |

## Conventions (acrobe-specific)

* **Op classes are frozen dataclasses, inputs only.** Future from
  `Batcher.post(op)` resolves to the natural result value
  (`int` for register reads, `None` for writes/runs, `bytes` for
  `mem_read`, etc.). No `op.data` mutation.
* **No properties with I/O side effects.** A property may read a
  cached value populated at `start()`; it must not issue register
  accesses. Anything that issues I/O is an `async def` method
  returning a future.
* **No tight polling.** All loops that wait for a hardware bit
  use `await asyncio.sleep(delay)` between probes, with a bounded
  total timeout.
* **`Db` registration via stacked decorators.** A component that
  appears under several IDs registers each with a separate
  `@db.register(...)` decorator (matches crobe).
* **One file per CoreSight component.** `coresight/<name>.py`,
  even if the body is a 10-line stub. Discoverability over compactness.
* **Logging at the right layer.** Each Node has `self.logger`; DP
  logs at trace level on op-by-op shifts, info on power-up; AP/MemAp
  log on configuration changes (CSW/TAR), not per-transfer.

## Open questions / deferred decisions

* **`@wire.op` UUIDs for DP/AP/MemAp ops.** Frozen dataclasses make
  this a one-line decoration when wire transport for these layers
  is wanted. No UUIDs minted yet; the conversation that introduces
  remote-DP need will set the seam.
* **Frequency capping per component.** Crobe's `FreqCapper` keys are
  per-component (e.g. minimum-implementation DPs cap at 8 MHz, slow
  PrimeCells cap their parent AP). acrobe's `FreqCapper` exists on
  `JtagInterface`; we'll register caps from DP/AP `start()` once
  a chip surfaces the constraint. Until then, no caps; the
  enumeration runs at chain-discovery cap (1 MHz today).
* **AP enumeration: ADIv6-only.** ADIv5's "AP index 0..255" is just
  a degenerate case of ADIv6's AP-base-address scheme — ADIv5 APs sit
  at addresses `index << 24` in the ADIv6 view. We implement ADIv6
  discovery (architected pointer chain via SELECT/SELECT1) only; it
  works against ADIv5 chips transparently, with the addresses falling
  on `n << 24` boundaries.
* **`PowerGate` retry from CLI.** Once Target framework lands, gates
  are walked by Target drivers. Pre-Target, an info subcommand
  `acrobe info gate retry <path>` is useful for debugging; ship it
  with the gate or defer to Target work — TBD.
* **Concrete component drivers' depth.** First pass for each is a
  Db registration + name. Register-level models (e.g. ITM stimulus
  ports, ETM trace config) follow demand.
