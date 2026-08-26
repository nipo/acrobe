"""Nordic Semiconductor CTRL-AP.

CTRL-AP is a vendor Access Port that lets the debugger trigger
device-level operations bypassing APPROTECT. Used for:

* RESET   (0x00) — assert / release SoC reset.
* ERASEALL (0x04) — start a full chip erase that unconditionally
  clears APPROTECT alongside flash + UICR. Takes ~175ms.
* ERASEALLSTATUS (0x08) — busy bit during the operation.
* APPROTECTSTATUS (0x0C) — bit 0 == 0 means debug is locked.

CTRL-AP IDR is 0x02880000 (Nordic JEP106 designer, class 0x4,
type 0x0). The `Ap.db` lookup masks REVISION + VARIANT so all
silicon rolls of nRF52 / nRF53 / nRF54 share one registration.
"""

from __future__ import annotations

import asyncio

from ..arm.ap import Ap


@Ap.db.register(0x02880000)
class CtrlAp(Ap):
    RESET_OFFSET           = 0x00
    ERASEALL_OFFSET        = 0x04
    ERASEALLSTATUS_OFFSET  = 0x08
    APPROTECTSTATUS_OFFSET = 0x0C

    POLL_PERIOD = 0.01

    def __init__(self, dp, base, idr=0, name=None):
        if name is None:
            if base & 0xFFFFFF == 0:
                name = f"ctrl-ap{base >> 24}"
            else:
                name = f"ctrl-ap@{base:08x}"
        super().__init__(dp, base, idr=idr, name=name)

    async def erase_all(self, *, timeout: float = 10.0) -> None:
        """Trigger a full chip erase and wait for completion.

        Bypasses APPROTECT; the device ends up blank and unlocked
        on completion. Polls ERASEALLSTATUS until the operation
        finishes."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        await self.reg_write(self.ERASEALL_OFFSET, 1)
        while True:
            status = await self.reg_read(self.ERASEALLSTATUS_OFFSET)
            if not (status & 1):
                return
            if loop.time() > deadline:
                raise TimeoutError(
                    f"CTRL-AP ERASEALL did not complete in {timeout}s")
            await asyncio.sleep(self.POLL_PERIOD)

    async def is_protected(self) -> bool:
        """True when APPROTECT is engaged (debug access denied)."""
        return not (await self.reg_read(self.APPROTECTSTATUS_OFFSET) & 1)

    async def assert_reset(self) -> None:
        await self.reg_write(self.RESET_OFFSET, 1)

    async def release_reset(self) -> None:
        await self.reg_write(self.RESET_OFFSET, 0)
