"""ARM SoC target dispatch.

This is the entry point for the bulk of ARM-based MCU targets.
Chip modules register their PartId(s) declaratively against
:data:`ArmSocTarget.db`; the single :func:`arm_soc_probe`
explorer asks the DP for its already-known chip identifier
(:meth:`Dp.chip_id`) and dispatches to the matching factory. No
speculative bus reads happen at probe time.

The mechanism mirrors crobe's `target/soc/arm_based/soc.py`
(``@SoC.db.register(PartId(...))`` + a single
``arm_soc_probe``) one-for-one. The rationale — and the bug
class this design avoids — is documented in
``docs/plans/target-discovery.md``.

Chip authors get a one-decorator API:

.. code-block:: python

    @ArmSocTarget.db.register(PartId(2, 0x44, 8))
    async def nrf52840_factory(dp):
        return await build_nrf52_target(dp, name="nRF52840")

The factory is called only after the constant PartId match
succeeds. It is therefore free to issue bus reads against
chip-specific peripheral addresses (flash sizing, BLE MAC, …)
— those reads are guaranteed to land on a chip that understands
them. The speculative-read-into-unknown-chip pathology that
broke RP2040 enumeration cannot occur here.

For multi-DP chips (RP2040), a chip-specific explorer registers
directly on :class:`Dp` at a lower precedence number — see
:mod:`acrobe.target.arm.rp2040_swd`. It must claim sibling DPs
so the generic probe doesn't refire on them in later passes.
"""

from __future__ import annotations

from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.dp import Dp
from ...component.arm.mem_ap import MemAp
from ...db import Db, NoMatch
from ...part_id import PartId
from ..target import Target
from .cortex_m import CortexMDebuggable, CortexMTarget


class ArmSocTarget(CortexMTarget):
    """Base for ARM-MCU Targets dispatched via :data:`db`.

    Chip modules subclass and register their PartIds:

    .. code-block:: python

        @ArmSocTarget.db.register(PartId(...))
        async def my_chip_factory(dp):
            ...

    The factory receives the :class:`Dp` whose ``chip_id()``
    matched. Re-fetch the matched ``PartId`` via
    ``dp.chip_id().partid`` if needed.
    """

    db: Db = Db(
        "ARM SoC by PartId",
        eq_func=lambda key, lookup: key.is_same_part(lookup))


@ArmSocTarget.db.register_default
def _unknown_arm_soc(dp):
    """Fallback factory: a generic Cortex-M Target with no chip-
    specific knowledge. Mirrors crobe's
    ``arm_based/soc.py:default_soc`` — the chip is identified
    enough to debug (CortexMDebuggable) but not enough to flash."""
    for ap in dp.children_of_class(MemAp):
        for rt in ap.children_of_class(RomTable):
            if not rt.children_of_class(Scs):
                continue
            chip = dp.chip_id()
            label = chip.partid.pretty() if chip else dp.name
            target = ArmSocTarget(f"arm-soc[{label}]")
            target.claim(dp, ap, rt)
            target.child_add(CortexMDebuggable.from_romtable(rt, ap))
            return target
    raise NoMatch("arm_soc_probe", f"no SCS under DP {dp.name}")


@Target.register(Dp, precedence=10000)
async def arm_soc_probe(dp):
    """Generic ARM SoC dispatch.

    Reads :meth:`Dp.chip_id` (already known — set during DP /
    ROM-table enumeration) and looks the resulting PartId up in
    :data:`ArmSocTarget.db`. Falls back to the default factory
    if no chip module has registered the PartId. Never issues a
    speculative bus read.

    Chip-specific explorers that need to override this generic
    path (e.g. multi-DP chips) register their own
    ``@Target.register(Dp, precedence=…)`` with a lower
    precedence number.
    """
    chip = dp.chip_id()
    if chip is None:
        raise NoMatch(
            "arm_soc_probe", f"DP {dp.name} has no chip identifier")
    return await ArmSocTarget.db.acall(chip.partid, dp)
