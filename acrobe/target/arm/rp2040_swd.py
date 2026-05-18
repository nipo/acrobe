"""Raspberry Pi RP2040 / RP2350 SWD multidrop target.

The RP2 family exposes one Debug Port per Cortex core plus a
rescue DP on a single shared SWD wire (ADIv5 multidrop, selected
via TARGETSEL). The multidrop bring-up
(:mod:`acrobe.protocol.swd`) already spawns one
:class:`SwDp` per responsive TARGETSEL on the wire; this module
turns that flat set of DPs into a single :class:`Rp2040Target`
with one :class:`CortexMDebuggable` per core.

Discovery hooks on :class:`Dp` at precedence 500 — lower than
the generic :func:`arm_soc_probe` at 10000, higher than the
chip-specific MCU explorers. The PartId match is a constant
compare against the RP2 family TPARTNO (0x1002 / 0x0004); no
bus reads needed. Once matched, the probe walks the parent
:class:`swd.Interface` for sibling :class:`SwDp` children and
claims them all, so the generic factory does not refire on
them in subsequent discovery passes.

Flash programming is not modelled here — the SWD path to RP2040
flash goes through complex XIP / QSPI sequencing best driven
from the BOOTSEL bootloader. Use the PICOBOOT-rooted
:class:`Rp2040Target` (sibling module ``rp2040.py``) for
flashing; this SWD target covers run-control only.
"""

from __future__ import annotations

from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.dp import Dp
from ...component.arm.mem_ap import MemAp
from ...component.arm.sw_dp import SwDp
from ...db import NoMatch
from ...part_id import PartId
from ...protocol import swd
from ..target import Target
from .cortex_m import CortexMDebuggable
from .soc import ArmSocTarget


# (TPARTNO, family-name) for chips whose multidrop layout this
# module understands. RP2350 is included pre-emptively — the
# multidrop shape is identical, TPARTNO is the only thing that
# differs between the families.
RP2_FAMILIES = {
    0x1002: "rp2040",
    0x0004: "rp2350",
}


class Rp2040SwdTarget(ArmSocTarget):
    """RP2040 / RP2350 run-control target accessed via SWD multidrop.

    Holds one :class:`CortexMDebuggable` per core DP. Loadable is
    deliberately absent — flash programming over SWD on RP2 needs
    BOOTSEL-driven XIP/QSPI sequencing that lives elsewhere."""


def _rp2_family(partid: PartId) -> str | None:
    """Return the family name (``"rp2040"`` / ``"rp2350"``) if
    ``partid`` matches a known RP2 chip; ``None`` otherwise.
    Compares the JEDEC + TPARTNO fields only — TINSTANCE
    (encoded in PartId.revision) varies per DP."""
    if (partid.jep106_bank == 9
            and partid.jep106_id == 0x13
            and partid.part_no in RP2_FAMILIES):
        return RP2_FAMILIES[partid.part_no]
    return None


@Target.register(Dp, precedence=500)
async def rp2040_swd_probe(dp):
    """Build one Rp2040SwdTarget out of every RP2 DP on this wire.

    Triggered once per Dp (whichever fires first wins). After
    claiming all sibling DPs, subsequent firings on the same wire
    raise :class:`NoMatch` because the chip's PartId is no longer
    on any unclaimed Dp.
    """
    chip = dp.chip_id()
    if chip is None:
        raise NoMatch("rp2040_swd_probe", f"DP {dp.name} has no chip_id")
    family = _rp2_family(chip.partid)
    if family is None:
        raise NoMatch(
            "rp2040_swd_probe",
            f"DP {dp.name} chip_id {chip.partid.pretty()} is not RP2")

    # Walk the parent swd.Interface for sibling SwDps. Each
    # MultidropSwDp carries its own TARGETSEL; we identify cores
    # by TARGETSEL's TINSTANCE (encoded as PartId.revision).
    iface = dp.parent_of_class(swd.Interface)
    if iface is None:
        # JTAG-DP or an unusual wiring — bail out, generic factory
        # picks it up as a single-DP Cortex-M target.
        raise NoMatch(
            "rp2040_swd_probe",
            f"DP {dp.name} is not on an swd.Interface")

    sibling_dps = iface.children_of_class(SwDp)
    cores: dict[int, SwDp] = {}    # TINSTANCE -> core DP
    rescue: SwDp | None = None
    for sib in sibling_dps:
        if sib.targetsel is None:
            continue
        sib_partid = PartId.from_idcode(sib.targetsel)
        if _rp2_family(sib_partid) != family:
            continue
        tinstance = sib_partid.revision
        if tinstance == 0xf:
            rescue = sib
        else:
            cores[tinstance] = sib

    if not cores:
        # Wire is in rescue-only state (chip stuck or held in
        # reset). Decline so the user can still target the rescue
        # DP directly to recover the chip.
        raise NoMatch(
            "rp2040_swd_probe",
            f"{family}: no core DP responded; only rescue present")

    target = Rp2040SwdTarget(family)
    target.claim(*cores.values())
    if rescue is not None:
        target.claim(rescue)

    # One Debuggable per core. Each core's DP carries its own
    # AHB-AP with its own CoreSight ROM Table + SCS subtree (the
    # multidrop layout is symmetric across cores).
    for tinstance in sorted(cores):
        core_dp = cores[tinstance]
        debuggable = _build_core_debuggable(
            core_dp, name=f"debug-core{tinstance}")
        if debuggable is None:
            target.logger.warning(
                "%s core%d: no SCS-bearing ROM table under AHB-AP — "
                "skipped",
                family, tinstance)
            continue
        target.child_add(debuggable)

    return target


def _build_core_debuggable(core_dp, *, name):
    """Construct a CortexMDebuggable from the first AHB-AP under
    ``core_dp`` that carries a ROM table with an SCS. Returns
    ``None`` if no suitable subtree exists (rescue-shaped DPs)."""
    for ap in core_dp.children_of_class(MemAp):
        for rt in ap.children_of_class(RomTable):
            if not rt.children_of_class(Scs):
                continue
            return CortexMDebuggable.from_romtable(rt, ap, name=name)
    return None
