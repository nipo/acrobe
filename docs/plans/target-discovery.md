# PLAN — ARM target discovery

Living document. Tracks the redesign of how ARM-based MCU targets
are discovered. Cross-validated against the synchronous sibling
project (crobe), which has shipped this exact shape for years:
see `crobe/target/soc/arm_based/soc.py:arm_soc_probe` and
the per-chip `@SoC.db.register(...)` decorations.

## Problem

Today every ARM MCU target hooks on `Dp` directly:

```python
@Target.register(Dp, precedence=500)
async def nrf52_probe(dp):
    ...
    for ap in dp.children_of_class(MemAp):
        try:
            part = await ap.read32(FICR_INFO_PART)
        except DpAccessFailure:
            continue
        ...
```

`acrobe/target/arm/nrf52.py:nrf52_probe` runs at precedence 500
against *every* `Dp` instance discovered on the wire, regardless
of which chip is actually attached. It does this by issuing live
bus reads to chip-specific addresses (`FICR_INFO_PART` in this
case). Same shape for `efm32_probe` and the generic
`cortex_m_generic_target`.

This blows up in three ways:

1. **Speculative reads against unmapped addresses fault.** On
   RP2040 the nrf52 probe reads 0x10000100 through an AHB-AP that
   isn't expecting that address; the chip responds with FAULT or
   no-driver. The probe catches `DpAccessFailure` and moves on,
   but the DP's `STICKYERR` is now latched. Subsequent
   transactions get FAULT until cleared by an explicit
   `STKERRCLR` — which neither the probe nor the framework
   issues.
2. **The probe count multiplies with chip count.** Adding the
   next nRF53 / STM32H7 / SAM-L11 means one more `Dp`-hooked
   probe firing against every chip on every adapter. The cost is
   bounded but never amortized.
3. **The information is already on the chip declaratively.**
   ADIv5/v6 puts a `TARGETID` on the DP (DPv2+) or a `PIDR` on
   the root ROM Table — both are read once during DP / ROM table
   discovery and exposed via `dp.chip_id()`. The probe phase has
   no reason to re-read anything.

## Design

One explorer for the bulk of ARM SoCs. One `Db` keyed on
`PartId`. Chip modules register declaratively. The discovery
phase never speculatively reads anything that isn't already
required by DP / ROM table enumeration.

### Layer 1 — the chip PartId is already known

`Dp.chip_id()` (`acrobe/component/arm/dp.py:464`) already returns
the best-available identifier with a documented preference
order:

1. `TARGETID` (DPv2+), when bit[0] is populated.
2. Root ROM Table `PIDR`.

It returns a `ChipId(partid, source)` or `None`. Everything below
consumes it.

### Layer 2 — the registry

A new module `acrobe/target/arm/soc.py` (mirrors crobe's
`target/soc/arm_based/soc.py`) defines:

```python
class ArmSocTarget(CortexMTarget):
    """Base for ARM-MCU Targets dispatched via PartId. Chip
    modules subclass and register their PartIds against
    :data:`ArmSocTarget.db`."""

    db: Db = Db(
        "ARM SoC by PartId",
        eq_func=lambda key, lookup: key.is_same_part(lookup))
```

The `eq_func` masks revision (the existing `PartId.is_same_part`
pattern), matching what `Tap.db` and `Ap.db` do.

### Layer 3 — the single Dp-level explorer

```python
@Target.register(Dp, precedence=10000)
async def arm_soc_probe(dp):
    """Generic ARM SoC dispatch.

    Asks the DP for its best chip identifier (which it already
    knows from TARGETID or root ROM Table PIDR; no wire activity
    here), looks it up in :data:`ArmSocTarget.db`, hands off to
    the registered factory. Never issues a speculative bus read.

    Chip-specific explorers that need a different precedence
    (e.g. RP2040's multi-DP aggregator) register their own
    `@Target.register(Dp, precedence=…)` and may delegate back
    into the Db when their constant-match succeeds."""
    chip = dp.chip_id()
    if chip is None:
        raise NoMatch("arm_soc_probe", f"DP {dp.name} has no chip_id")
    return await ArmSocTarget.db.acall(chip.partid, dp)
```

Precedence 10000 means: any chip-specific explorer with a lower
number gets first refusal. The bulk of MCUs don't need their own
explorer at all — they live entirely inside this Db.

### Layer 4 — chip registrations are declarative

```python
# acrobe/target/arm/nrf52.py
@ArmSocTarget.db.register(PartId(2, 0x44, 6))   # nRF52832
@ArmSocTarget.db.register(PartId(2, 0x44, 8))   # nRF52840
@ArmSocTarget.db.register(PartId(2, 0x44, 0xd)) # nRF52833
class Nrf52Target(ArmSocTarget):
    def __init__(self, dp):
        super().__init__(self._name_from_partid(dp.chip_id().partid),
                         dp)
```

Each chip module: a constant PartId per variant, plus a factory.
No wire reads at probe time. Adding a new chip = one decorator +
a class.

### Layer 5 — default and secondary discrimination

`ArmSocTarget.db.register_default` covers chips we don't know:

```python
@ArmSocTarget.db.register_default
def unknown_soc(partid, dp):
    target = ArmSocTarget(
        f"Unknown ARM SoC {partid.pretty()}", dp)
    target.logger.note(
        "No chip-specific factory for %s; using generic Cortex-M",
        partid.pretty())
    return target
```

Vendor families that share one PartId across SKUs (NXP LPC, ST
STM32, Cypress PSoC) register the shared PartId once and do
second-level discrimination *inside* the factory:

```python
@ArmSocTarget.db.register(*[PartId(0, 0x20, did)
                            for did in Stm32Info.parts.keys()
                            if did])
class Stm32Target(ArmSocTarget):
    def __init__(self, dp):
        super().__init__("STM32", dp)
        self.info = Stm32Info.from_soc(self)
        self.name = self.info.name
```

This is the only place speculative bus reads live, and they
only run on a confirmed positive PartId match for a family
known to require them. Sticky-error hygiene becomes the
factory's responsibility (clear before, clear after).

### Layer 6 — multi-DP chips

Chips that expose one logical SoC across several DPs hook on
`Dp` at a *lower* precedence than the generic explorer, then
walk the wire to confirm the multi-DP shape. RP2040 is the
reference case:

```python
# acrobe/target/arm/rp2040.py
@Target.register(Dp, precedence=500)
async def rp2040_probe(dp):
    chip = dp.chip_id()
    if chip is None or not chip.partid.is_same_part(
            PartId(9, 0x13, 0x1002)):
        raise NoMatch("rp2040_probe", "not RP2040")
    # Constant-match succeeded. Verify the multi-DP shape on
    # this wire: must find core0 + core1 + rescue DPs sharing
    # the same swd.Interface parent.
    iface = dp.parent_of_class(swd.Interface)
    sibling_dps = iface.children_of_class(SwDp)
    cores = {sw.targetsel: sw for sw in sibling_dps
             if sw.targetsel is not None}
    targetsel_core0 = int(PartId(9, 0x13, 0x1002, 0))
    targetsel_core1 = int(PartId(9, 0x13, 0x1002, 1))
    if targetsel_core0 not in cores or targetsel_core1 not in cores:
        raise NoMatch("rp2040_probe", "incomplete multi-DP shape")
    target = Rp2040Target(...)
    target.claim(dp, *sibling_dps)  # so the generic explorer
                                     # doesn't re-fire on siblings
    return target
```

Precedence 500 vs 10000 means RP2040 wins on any of its three
DPs. The `target.claim(...)` on every sibling DP ensures the
generic ARM SoC explorer skips them in subsequent passes —
fixed-point loop terminates cleanly.

This is exactly crobe's
`target/soc/arm_based/rp2040.py:rp2040_probe` pattern, just
adapted to acrobe's claim model.

## Migration

### Step 1 — introduce `ArmSocTarget` + `arm_soc_probe`

Create `acrobe/target/arm/soc.py` with the class and explorer
above. Empty Db at start. Register the explorer at precedence
10000. With no chip registrations yet, the explorer hits the
default factory ("Unknown ARM SoC …") for every Dp. Validate
end-to-end on a real chip (nRF52 dev board, RP2040 single-core
mode) — at this point all chips look like "Unknown" but the
shape works.

### Step 2 — migrate nRF52 first

Rewrite `acrobe/target/arm/nrf52.py`:

- Remove `@Target.register(Dp, precedence=500)`.
- Replace with `@ArmSocTarget.db.register(PartId(2, 0x44, X))`
  for each known variant.
- The factory's old wire-read of `FICR_INFO_PART` becomes a
  *post-construction* refinement (look up flash/ram size, etc.)
  — bus reads happen after the PartId match has already
  succeeded, and only on chips that need them.
- Locked-chip detection (`APPROTECT` via CTRL-AP) moves out of
  the probe path. Check sticky state once *after* match, inside
  the factory.

### Step 3 — migrate EFM32

Same pattern; `efm32_probe` becomes `@ArmSocTarget.db.register
(PartId(6, 0x73, 0x1))` (or whichever PartIds the SiLabs
families actually carry).

### Step 4 — RP2040

Move the `Rp2040Target` build under a `@Target.register(Dp,
precedence=500)` that constant-matches on PartId and walks
siblings (see Layer 6 above). The existing `Rp2040RescueDp`
stays as a `SwDp` subclass — it's a component-tree concern, not
a target-discovery one.

The PICOBOOT-rooted RP2040 target (`target/arm/rp2040.py`'s
existing `@Target.register(Picoboot)` explorer) is untouched —
it's a different shape entirely (USB bootloader, not SWD).

### Step 5 — drop the generic Cortex-M Dp explorer

`cortex_m_generic_target` at precedence 10000 in
`acrobe/target/arm/cortex_m.py` becomes redundant once
`arm_soc_probe`'s default factory does the same job. Delete it.

### Step 6 — STM32 (if it lands)

When STM32 support is ported from crobe, it slots in as a
single registration covering every DEV_ID variant, with
`Info.from_soc` running the DBGMCU_IDCODE secondary read
inside the factory (the only place in the new architecture
where speculative bus reads against a peripheral remain).

## What changes for callers

Nothing user-visible. `acrobe info target -r ...` still returns
the right Target. The CLI doesn't care which explorer fired.

## What changes for chip authors

A new chip is a constant + a class:

```python
@ArmSocTarget.db.register(PartId(...))
class FooTarget(ArmSocTarget):
    def __init__(self, dp):
        super().__init__("Foo", dp)
        # any factory-time setup goes here — runs only on a
        # confirmed PartId match
```

No `@Target.register`, no `precedence`, no defensive try/except
around speculative reads, no DpAccessFailure plumbing in the
probe path.

## Open items captured for later

- **PartId masking knobs.** Some vendors put per-SKU info in
  `PartId.revision`; others use it for silicon revisions we
  want to mask. The default `is_same_part` masks revision — if
  a vendor needs revision-sensitive routing, they register
  multiple PartIds explicitly (cheap) or use a custom eq_func
  on a chip-private Db (e.g. NXP's SIM_SDID variant table).
- **JtagDp parity.** This plan assumes `Dp` covers both
  `SwDp` and `JtagDp`. JTAG-DP also exposes TARGETID-equivalent
  identifiers; the `Dp.chip_id()` aggregation already handles
  both. Sanity-check on a JTAG-only target (Cortex-A SoC with
  CoreSight JTAG-AP) when the first such chip lands.
- **Locked debug.** Today nrf52_probe builds a separate
  "locked target" view via `ctrl_ap.is_protected()`. Under the
  new model, the locked-vs-unlocked decision moves into the
  `Nrf52Target` factory — same registration, factory branches
  on the lock state and returns one Target shape or the other.
- **`@SoC.db.register_default` semantics.** Crobe's default
  builds an "Unknown SoC" target carrying whatever PartId it
  matched on. We do the same; the explorer's
  `ArmSocTarget.db.acall(partid, dp)` passes partid as the
  argument so the default factory can name itself.
