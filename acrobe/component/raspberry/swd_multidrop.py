"""SWD multidrop TARGETSEL registrations for Raspberry Pi silicon.

Importing this module populates :data:`swd.Interface.targetsel_db`
with the TARGETID values that ``swd(multidrop=scan)`` walks. Each
RP2040 / RP2350 die exposes one Debug Port per Cortex core plus a
rescue DP for recovering a hung chip, all sharing a single SWD
wire and selected via TARGETSEL.

TARGETID layout (ADIv5/v6 multidrop):

    bit[0]      = RAO
    bit[11:1]   = TDESIGNER (JEDEC continuation + ID)
    bit[27:12]  = TPARTNO
    bit[31:28]  = TINSTANCE (core / DP index on this chip)

For Raspberry Pi: TDESIGNER = (bank=9, id=0x13), TPARTNO=0x1002 on
RP2040, 0x0004 on RP2350. TINSTANCE distinguishes the per-core
DPs from the rescue DP."""

from ...protocol import swd
from ...part_id import PartId
from ..arm import dp as dpmod
from ..arm.sw_dp import SwDp


def _targetsel(jep_bank: int, jep_id: int, part_no: int,
               instance: int) -> int:
    """Compose a TARGETSEL value matching the ADIv5 TARGETID
    layout. PartId packs JEDEC + part identifiers identically, so
    we delegate to it and slot the instance into TREVISION."""
    return int(PartId(jep_bank, jep_id, part_no, instance))


# RP2040 — dual Cortex-M0+. TPARTNO 0x1002.
swd.Interface.targetsel_db.register(
    _targetsel(9, 0x13, 0x1002, 0))("rp2040-core0")
swd.Interface.targetsel_db.register(
    _targetsel(9, 0x13, 0x1002, 1))("rp2040-core1")
# Rescue DP — TINSTANCE 0xf. Reachable when the cores have been
# wedged (e.g. user firmware hangs with the bus locked); the
# rescue DP can force a CTRL/STAT-driven power cycle on the chip.
swd.Interface.targetsel_db.register(
    _targetsel(9, 0x13, 0x1002, 0xf))("rp2040-rescue")

# RP2350 — dual Cortex-M33 (or RISC-V Hazard3). TPARTNO 0x0004.
swd.Interface.targetsel_db.register(
    _targetsel(9, 0x13, 0x0004, 0))("rp2350-core0")
swd.Interface.targetsel_db.register(
    _targetsel(9, 0x13, 0x0004, 1))("rp2350-core1")
swd.Interface.targetsel_db.register(
    _targetsel(9, 0x13, 0x0004, 0xf))("rp2350-rescue")


@swd.Interface.db.register(0x10212927)
class Rp2040RescueDp(SwDp):
    """RP2040 / RP2350 Rescue DP.

    The rescue DP differs from a regular ARM SW-DP in two crucial
    ways:

    1. Its ``CDBGPWRUPREQ`` doesn't power the debug fabric — it
       triggers a *full reset of the cores* while asserted. The
       generic :meth:`Dp.start` flow ("assert PWRUP_REQ, poll
       PWRUP_ACK") therefore holds the cores hostage, masking
       them on subsequent bring-ups via the per-core multidrop
       DPs.
    2. It has no APs. There is nothing to enumerate beneath it.

    Default :meth:`start` is therefore a no-op: it makes the
    rescue DP visible in the tree (so the user can address it
    explicitly to recover a stuck chip via :meth:`rescue_pulse`)
    without disturbing the cores."""

    async def start(self):
        # Deliberately no super().start(): we don't power up the
        # rescue DP, we don't enumerate APs, we just exist as a
        # handle for the user to call :meth:`rescue_pulse` on.
        return

    async def rescue_pulse(self):
        """Pulse ``CDBGPWRUPREQ`` to force the chip out of a hung
        state. Per RP2040 datasheet, raising CDBGPWRUPREQ on the
        rescue DP holds the cores in reset; dropping it lets them
        come back online with the bootloader running."""
        import asyncio
        await self.post(dpmod.Abort(self.ABORT_ALL))
        await self.post(dpmod.DpWrite(
            self.CTRL_STAT, self.CDBGPWRUPREQ))
        await self.post(dpmod.Run(32))
        # Hold reset for a moment so the chip-side state machine
        # has time to latch the request.
        await asyncio.sleep(0.001)
        await self.post(dpmod.DpWrite(self.CTRL_STAT, 0))
        await self.post(dpmod.Run(32))
        self.logger.note("Rescue pulse complete")
