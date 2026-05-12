"""Nordic Semiconductor nRF52 family target.

nRF52 flash programming uses the on-chip NVMC peripheral, accessed
directly through the Mem-AP — no on-target stub code required.
This keeps the V1 implementation Puppet-free.

Detection runs against the AHB-AP under each DP: read
FICR.INFO.PART (0x10000100) and match against the known
part-number table. Unknown parts decline so the generic
Cortex-M target picks them up.

CTRL-AP ERASEALL (the path that works through APPROTECT lock) is
out of S2b scope — for now, ERASEALL falls back to per-page
erases through NVMC, which requires debug access.
"""

from __future__ import annotations

import asyncio

from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.dp import Dp, DpAccessFailure
from ...component.arm.mem_ap import MemAp
from ...db import NoMatch
from ..loadable import Loadable
from ..region import Flash
from ..target import Target
from .cortex_m import CortexMDebuggable, CortexMTarget


# NVMC controller registers.
NVMC_BASE       = 0x4001E000
NVMC_READY      = NVMC_BASE + 0x400
NVMC_READYNEXT  = NVMC_BASE + 0x408
NVMC_CONFIG     = NVMC_BASE + 0x504
NVMC_ERASEPAGE  = NVMC_BASE + 0x508
NVMC_ERASEALL   = NVMC_BASE + 0x50C
NVMC_ERASEUICR  = NVMC_BASE + 0x514

NVMC_CONFIG_REN = 0
NVMC_CONFIG_WEN = 1
NVMC_CONFIG_EEN = 2

NVMC_READY_BIT  = 1 << 0

# FICR (Factory Information Configuration Registers).
FICR_BASE         = 0x10000000
FICR_CODEPAGESIZE = FICR_BASE + 0x010
FICR_CODESIZE     = FICR_BASE + 0x014
FICR_DEVICEID0    = FICR_BASE + 0x060
FICR_DEVICEID1    = FICR_BASE + 0x064
FICR_INFO_PART    = FICR_BASE + 0x100
FICR_INFO_VARIANT = FICR_BASE + 0x104
FICR_INFO_PACKAGE = FICR_BASE + 0x108
FICR_INFO_RAM     = FICR_BASE + 0x10C
FICR_INFO_FLASH   = FICR_BASE + 0x110

UICR_BASE = 0x10001000

# Known nRF52 family part numbers (from FICR.INFO.PART).
NRF52_PARTS = {
    0x52805: "nRF52805",
    0x52810: "nRF52810",
    0x52811: "nRF52811",
    0x52820: "nRF52820",
    0x52832: "nRF52832",
    0x52833: "nRF52833",
    0x52840: "nRF52840",
}


class NvmcFlash(Flash):
    """nRF52 flash region driven by the NVMC peripheral.

    Reads pass through Mem-AP `mem_read` (flash is memory-mapped at
    the region's base). Erase and write toggle NVMC.CONFIG and poll
    NVMC.READY between transactions. CONFIG is restored to REN on
    every exit path so a failed erase doesn't leave the device in
    write/erase mode."""

    POLL_PERIOD = 0.001

    def __init__(self, name, address, size, mem_ap, *,
                 page_size: int):
        super().__init__(name, address, size,
                         write_page_size=page_size,
                         erase_page_sizes=[page_size])
        self.mem_ap = mem_ap

    async def read(self, offset, size):
        return await self.mem_ap.mem_read(self.address + offset, size)

    async def erase(self, offset, size):
        if offset % self.write_page_size or size % self.write_page_size:
            raise ValueError(
                f"NvmcFlash erase must be page-aligned "
                f"(page={self.write_page_size:#x})")
        await self.__set_config(NVMC_CONFIG_EEN)
        try:
            addr = self.address + offset
            end = addr + size
            while addr < end:
                await self.mem_ap.write32(NVMC_ERASEPAGE, addr)
                await self.__wait_ready()
                addr += self.write_page_size
        finally:
            await self.__set_config(NVMC_CONFIG_REN)
        if offset == 0 and size == self.size:
            self.is_blank = True

    async def write(self, offset, data):
        if offset % 4 or len(data) % 4:
            raise ValueError("NvmcFlash write must be word-aligned")
        await self.__set_config(NVMC_CONFIG_WEN)
        try:
            await self.mem_ap.mem_write(self.address + offset, data)
            await self.__wait_ready()
        finally:
            await self.__set_config(NVMC_CONFIG_REN)

    async def __set_config(self, value: int):
        await self.mem_ap.write32(NVMC_CONFIG, value)
        await self.__wait_ready()

    async def __wait_ready(self, timeout: float = 5.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            ready = await self.mem_ap.read32(NVMC_READY)
            if ready & NVMC_READY_BIT:
                return
            if loop.time() > deadline:
                raise TimeoutError(
                    f"NVMC not ready after {timeout}s")
            await asyncio.sleep(self.POLL_PERIOD)


class Nrf52Target(CortexMTarget):
    """nRF52 family target — Cortex-M4 (M4F on -840/-833) with NVMC
    flash programming."""


@Target.register(Dp, precedence=500)
async def nrf52_probe(dp):
    """Probe a DP for an nRF52-family chip.

    Walks the DP's AHB-AP children, reads FICR.INFO.PART, and
    matches against the known part table. Declines (NoMatch) for
    unknown parts so the generic Cortex-M target catches them.
    """
    aps = dp.children_of_class(MemAp)
    if not aps:
        raise NoMatch("nrf52_probe", "no MemAp under DP")
    # Try every MemAp — multi-AP layouts may park flash MMIO behind
    # the second AP. The first AP that successfully reads FICR.INFO.PART
    # with a matching part number wins.
    for ap in aps:
        try:
            part = await ap.read32(FICR_INFO_PART)
        except DpAccessFailure:
            continue
        if part not in NRF52_PARTS:
            continue
        return await _build_nrf52_target(dp, ap, part)
    raise NoMatch("nrf52_probe", "no nRF52 found behind DP")


async def _build_nrf52_target(dp, ap, part):
    page_size = await ap.read32(FICR_CODEPAGESIZE)
    page_count = await ap.read32(FICR_CODESIZE)
    flash_size = page_size * page_count

    rom_tables = ap.children_of_class(RomTable)
    rt = next((r for r in rom_tables if r.children_of_class(Scs)), None)
    if rt is None:
        raise NoMatch("nrf52_probe", "no SCS under MemAp")

    name = NRF52_PARTS[part]
    target = Nrf52Target(name)
    target.claim(dp, ap, rt)
    target.child_add(CortexMDebuggable.from_romtable(rt, ap))

    loadable = Loadable("main")
    loadable.child_add(
        NvmcFlash("code", 0x00000000, flash_size, ap, page_size=page_size))
    target.child_add(loadable)
    return target
