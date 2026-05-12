# JTAG chain refresh: known gaps

This is a living note about deferred work in the chain-refresh
framework (`acrobe/protocol/jtag.py`: `Chain.tlr_and_refresh`,
`tap_detach` / `tap_reattach`, `Tap.pre_tlr` / `post_tlr`,
`ChainContext.controller` / `gated`).

The framework is in place and known to handle:

* Cold-init discovery + controller hand-over (Agilex5 claims its
  HPS DP, IcePick gets sub-TAPs registered with `controller=self`).
* TLR survival for IcePick — DP and AON keep Python identity
  across `chain.tlr_and_refresh()`.
* Agilex bitstream load that crosses the chain-shrink boundary —
  pre-stream refresh drops the now-vanished HPS DP, post-stream
  refresh picks it back up.

What follows are cases the current implementation does **not**
handle. The relevant fix in each case is sketched, but the
prerequisite is hardware we don't yet have.

## Multi-DP / same-IDCODE ambiguity

### Setup

A chain with **two TAPs of the same IDCODE**, one of them gated
by a controller. Concretely:

  `[Agilex, HPS-DPv3, Other-SoC-DPv3]`

Where `HPS-DPv3` is the ARM debug port surfaced by Agilex's HPS
when the bitstream enables it, and `Other-SoC-DPv3` is a separate
SoC (or another HPS) wired into the same JTAG chain *after* the
Agilex.

Both DPs report IDCODE `0x4ba06477` (ADIv6 / DPv3).

### Why the current matcher fails

After a successful HPS-enabled load, `Agilex5.load()` drives a
TLR refresh. The probe sees a new chain of:

  `[Agilex(10), DPv3(4), DPv3(4)]`

The matcher in `Chain._match_identities` walks existing TAPs in
old chain order. Existing chain is
`[Agilex(0), Other-SoC-DPv3(10)]` (HPS-DPv3 was detached
pre-stream). When `Other-SoC-DPv3` tries to claim a slot it finds
two unclaimed slots whose `(idcode, irlen)` match — slot 1 and
slot 2 — and raises `RefreshAmbiguity`.

There's no inherent ambiguity *for a human*: slot 1 is the new
HPS DP and slot 2 is the Other-SoC DP (its old position relative
to TDO is preserved). But the greedy matcher can't tell, so it
fails hard rather than guess. Per the design rule "fail loudly,
refine later", that's the right behaviour until we have hardware
to validate against.

### Sketched fix (when the board lands)

Refine `Chain._match_identities` into a two-pass matcher:

  1. **Stable taps first** — existing TAPs whose
     `ChainContext.controller` is `None` claim slots greedily in
     **old chain order**. Preserve relative TDO-to-TDI position
     among the surviving chain-owned TAPs. In the multi-DP case
     this lets `Other-SoC-DPv3` (chain-owned) claim the slot that
     keeps it "furthest from TDO among DPs".
  2. **Controlled taps second** — for each controller in the
     chain, its gated sub-TAPs claim from the remaining slots,
     anchored by their expected position relative to the
     controller (the HPS-DPv3 is immediately TDI-side of the
     Agilex, so it claims slot 1 of the new chain).

Stage 1 is enough for the simple HPS-up / HPS-down case we've
exercised so far. Stage 2 is what we need for multi-DP. Adding
"anchor by controller offset" without breaking the
single-controller path means giving `Tap` a hook like:

  ```python
  def expected_slot_offset(self) -> int:
      """For a gated Tap: signed slot offset from its
      controller's slot. Positive = TDI-side.
      Default: 0 / None for chain-owned."""
  ```

The HPS-DP would return `+1` (one slot TDI-side of Agilex). An
IcePick sub-TAP would return offsets relative to the IcePick
(more elaborate because IcePick has internal key ordering).

### What to do before the board exists

When `RefreshAmbiguity` is raised in production code, the user
gets a hard failure. That's the explicit choice. If a user hits
it on a chain we *haven't* anticipated, that's a signal we need
the multi-controller anchor logic above — pin the chain
configuration in `tests/test_jtag.py` and refine.

## Other deferred items

* **`IcePick` cool-down via auto-wake on access** — track
  per-TAP idle time; detach sub-TAPs that have been idle past a
  configurable threshold. First op on a detached sub-TAP
  transparently re-enables it via the controller.
* **RBF preamble inspection for HPS-enabled detection** —
  currently the only signal is "do a TLR refresh and see whether
  a new TAP appeared". A faster probe by parsing the bitstream
  header would let drivers pre-plan. (Pending shareable
  reference material from the user.)
