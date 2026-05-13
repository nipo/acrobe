"""Silicon Labs (Energy Micro) EFM32 / EFR32 family target.

EFM32 flash programming uses the on-chip MSC peripheral, accessed
directly through the Mem-AP — same MMIO path as crobe's slow path,
but always used (no Puppet trampoline in V1). Performance is fine
on small Series 0 parts; Series 1GG with ~2 MiB of flash will be
visibly slower than vendor tools until the Puppet substrate lands.

Detection reads the DI (Device Information) block at 0x0FE081B0 +
0x4C through the AHB-AP. The family byte selects the MSC layout
(base address + LOCK offset) and whether MSC has a mass-erase
fast path. Unknown families decline so the generic Cortex-M
target catches them.

The MSC MASSLOCK / AAP-unlock paths for an APPROTECT-equivalent
locked chip are not implemented here. Locked chips will fail at
the DI read and fall through to the generic Cortex-M target with
no Loadable.
"""

from __future__ import annotations

import asyncio
import struct

from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.dp import Dp, DpAccessFailure
from ...component.arm.memory import BusRam
from ...component.arm.mem_ap import MemAp
from ...db import NoMatch
from ..debuggable import Debuggable
from ..loadable import Loadable
from ..memory import Memory
from ..region import Flash, Ram
from ..target import Target
from .cortex_m import CortexMDebuggable, CortexMTarget


class Part:
    """One row of the EFM32 family table."""

    def __init__(self, name, flash_class,
                 flash_page_size=None, bootloader=False):
        self.name = name
        self.flash_class = flash_class
        self.flash_page_size = flash_page_size
        self.bootloader = bootloader


class EfmFlash(Flash):
    """EFM32 flash region driven by the MSC peripheral.

    Reads pass through Mem-AP `mem_read` (flash is memory-mapped at
    the region base). Erase and write drive the MSC registers
    directly; `MSC_LOCK` is unlocked for the duration of one
    erase / write call and re-locked on every exit path.

    Subclasses set `MSC` (the controller base) and `MSC_LOCK`
    (offset to the lock register within `MSC`) for the chip's
    series; the rest of the register layout is identical across
    Series 0 / 1 / 1GG.
    """

    MSC = None
    MSC_LOCK = None

    MSC_LOCK_MAGIC = 0x1B71
    MSC_MASSLOCK_MAGIC = 0x631A

    MSC_WRITECTRL = 0x008
    MSC_WRITECMD  = 0x00C
    MSC_ADDRB     = 0x010
    MSC_WDATA     = 0x018
    MSC_STATUS    = 0x01C
    MSC_MASSLOCK  = 0x054

    MSC_WRITECTRL_WREN      = 1 << 0
    MSC_WRITECMD_LADDRIM    = 1 << 0
    MSC_WRITECMD_ERASEPAGE  = 1 << 1
    MSC_WRITECMD_WRITEONCE  = 1 << 3
    MSC_WRITECMD_ERASEMAIN0 = 1 << 8
    MSC_WRITECMD_ERASEMAIN1 = 1 << 9

    MSC_STATUS_BUSY = 1 << 0

    POLL_PERIOD = 0.001

    # Mass-erase fast path via MSC_WRITECMD_ERASEMAIN0/1. Overridden
    # to False on Series 0G — that variant has no ERASEMAIN command
    # and must fall back to per-page erase.
    has_mass_erase = True

    def __init__(self, name, address, size, mem_ap, *, page_size):
        super().__init__(name, address, size,
                         write_page_size=page_size,
                         erase_page_sizes=[page_size])
        self.mem_ap = mem_ap

    async def read(self, offset, size):
        return await self.mem_ap.mem_read(self.address + offset, size)

    async def erase(self, offset, size):
        if offset % self.write_page_size or size % self.write_page_size:
            raise ValueError(
                f"EfmFlash erase must be page-aligned "
                f"(page={self.write_page_size:#x})")
        full_region = (offset == 0 and size == self.size)
        if full_region and self.has_mass_erase:
            await self.mass_erase()
            return
        await self.unlock()
        try:
            await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECTRL),
                                      self.MSC_WRITECTRL_WREN)
            n_pages = size // self.write_page_size
            if n_pages > 1:
                with self.progress("erase", n_pages, "pages") as bar:
                    await self.erase_pages(offset, n_pages, bar)
            else:
                await self.erase_pages(offset, n_pages, None)
        finally:
            await self.lock()
        if full_region:
            self.is_blank = True

    async def erase_pages(self, offset, n_pages, bar):
        addr = self.address + offset
        for _ in range(n_pages):
            await self.mem_ap.write32(self.msc_reg(self.MSC_ADDRB), addr)
            await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECMD),
                                      self.MSC_WRITECMD_LADDRIM)
            await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECMD),
                                      self.MSC_WRITECMD_ERASEPAGE)
            await self.wait_idle()
            addr += self.write_page_size
            if bar is not None:
                bar.advance(1)

    async def write(self, offset, data):
        if offset % 4 or len(data) % 4:
            raise ValueError("EfmFlash write must be word-aligned")
        await self.unlock()
        try:
            await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECTRL),
                                      self.MSC_WRITECTRL_WREN)
            addr = self.address + offset
            for word_index in range(0, len(data), 4):
                word = struct.unpack_from("<I", data, word_index)[0]
                await self.mem_ap.write32(self.msc_reg(self.MSC_ADDRB),
                                          addr + word_index)
                await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECMD),
                                          self.MSC_WRITECMD_LADDRIM)
                await self.mem_ap.write32(self.msc_reg(self.MSC_WDATA),
                                          word)
                await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECMD),
                                          self.MSC_WRITECMD_WRITEONCE)
                await self.wait_idle()
        finally:
            await self.lock()

    async def mass_erase(self):
        """Chip-wide flash erase via MSC_WRITECMD_ERASEMAIN0 (+ MAIN1
        on parts with >= 512 KiB).

        Requires both MSC_LOCK and MSC_MASSLOCK to be unlocked.
        Series 0G subclasses this away (no ERASEMAIN command on
        that variant).
        """
        await self.unlock()
        await self.mass_unlock()
        try:
            await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECTRL),
                                      self.MSC_WRITECTRL_WREN)
            await self.wait_idle()
            await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECMD),
                                      self.MSC_WRITECMD_ERASEMAIN0)
            await self.wait_idle(timeout=20.0)
            if self.size >= (1 << 19):
                await self.mem_ap.write32(self.msc_reg(self.MSC_WRITECMD),
                                          self.MSC_WRITECMD_ERASEMAIN1)
                await self.wait_idle(timeout=20.0)
        finally:
            await self.mass_lock()
            await self.lock()
        self.is_blank = True

    async def unlock(self):
        await self.mem_ap.write32(self.msc_reg(self.MSC_LOCK),
                                  self.MSC_LOCK_MAGIC)

    async def lock(self):
        await self.mem_ap.write32(self.msc_reg(self.MSC_LOCK), 0)

    async def mass_unlock(self):
        await self.mem_ap.write32(self.msc_reg(self.MSC_MASSLOCK),
                                  self.MSC_MASSLOCK_MAGIC)

    async def mass_lock(self):
        await self.mem_ap.write32(self.msc_reg(self.MSC_MASSLOCK), 0)

    async def wait_idle(self, timeout: float = 5.0):
        """Poll MSC_STATUS until BUSY clears or `timeout` elapses."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            status = await self.mem_ap.read32(self.msc_reg(self.MSC_STATUS))
            if not (status & self.MSC_STATUS_BUSY):
                return
            if loop.time() > deadline:
                raise TimeoutError(
                    f"MSC busy after {timeout}s (status={status:#x})")
            await asyncio.sleep(self.POLL_PERIOD)

    def msc_reg(self, offset: int) -> int:
        return self.MSC + offset


class EfmFlashSeries0(EfmFlash):
    MSC = 0x400C0000
    MSC_LOCK = 0x3C


class EfmFlashSeries0G(EfmFlashSeries0):
    """EFM32G — Series 0 without MSC mass-erase support.

    Full-region erase falls back to per-page through the inherited
    `erase()` path because `has_mass_erase` is False.
    """

    has_mass_erase = False


class EfmFlashSeries1(EfmFlash):
    MSC = 0x400E0000
    MSC_LOCK = 0x40


class EfmFlashSeries1GG(EfmFlash):
    MSC = 0x40000000
    MSC_LOCK = 0x40


# DEVICE_FAMILY → Part. Family number is the byte at
# DI_BASE + 0x4D (the family field of the DI_PART register).
PARTS = {
     16: Part("EFR32MG1P",  EfmFlashSeries1),
     17: Part("EFR32MG1B",  EfmFlashSeries1),
     18: Part("EFR32MG1V",  EfmFlashSeries1),
     19: Part("EFR32BG1P",  EfmFlashSeries1),
     20: Part("EFR32BG1B",  EfmFlashSeries1),
     21: Part("EFR32BG1V",  EfmFlashSeries1),
     25: Part("EFR32FG1P",  EfmFlashSeries1),
     26: Part("EFR32FG1B",  EfmFlashSeries1),
     27: Part("EFR32FG1V",  EfmFlashSeries1),
     28: Part("EFR32MG12P", EfmFlashSeries1, bootloader=True),
     29: Part("EFR32MG12B", EfmFlashSeries1, bootloader=True),
     30: Part("EFR32MG12V", EfmFlashSeries1, bootloader=True),
     31: Part("EFR32BG12P", EfmFlashSeries1, bootloader=True),
     32: Part("EFR32BG12B", EfmFlashSeries1, bootloader=True),
     33: Part("EFR32BG12V", EfmFlashSeries1, bootloader=True),
     37: Part("EFR32FG12P", EfmFlashSeries1, bootloader=True),
     38: Part("EFR32FG12B", EfmFlashSeries1, bootloader=True),
     39: Part("EFR32FG12V", EfmFlashSeries1, bootloader=True),
     40: Part("EFR32MG13P", EfmFlashSeries1, bootloader=True),
     41: Part("EFR32MG13B", EfmFlashSeries1, bootloader=True),
     42: Part("EFR32MG13V", EfmFlashSeries1, bootloader=True),
     43: Part("EFR32BG13P", EfmFlashSeries1, bootloader=True),
     44: Part("EFR32BG13B", EfmFlashSeries1, bootloader=True),
     45: Part("EFR32BG13V", EfmFlashSeries1, bootloader=True),
     46: Part("EFR32ZG13P", EfmFlashSeries1, bootloader=True),
     49: Part("EFR32FG13P", EfmFlashSeries1, bootloader=True),
     50: Part("EFR32FG13B", EfmFlashSeries1, bootloader=True),
     51: Part("EFR32FG13V", EfmFlashSeries1, bootloader=True),
     52: Part("EFR32MG14P", EfmFlashSeries1, bootloader=True),
     53: Part("EFR32MG14B", EfmFlashSeries1, bootloader=True),
     54: Part("EFR32MG14V", EfmFlashSeries1, bootloader=True),
     55: Part("EFR32BG14P", EfmFlashSeries1, bootloader=True),
     56: Part("EFR32BG14B", EfmFlashSeries1, bootloader=True),
     57: Part("EFR32BG14V", EfmFlashSeries1, bootloader=True),
     58: Part("EFR32ZG14P", EfmFlashSeries1, bootloader=True),
     61: Part("EFR32FG14P", EfmFlashSeries1, bootloader=True),
     62: Part("EFR32FG14B", EfmFlashSeries1, bootloader=True),
     63: Part("EFR32FG14V", EfmFlashSeries1, bootloader=True),
     71: Part("EFM32G",     EfmFlashSeries0G),
     72: Part("EFM32GG",    EfmFlashSeries0),
     73: Part("EFM32TG",    EfmFlashSeries0),
     74: Part("EFM32LG",    EfmFlashSeries0,
              flash_page_size=2048),  # errata DI_E101
     75: Part("EFM32WG",    EfmFlashSeries0),
     76: Part("EFM32ZG",    EfmFlashSeries0),
     77: Part("EFM32HG",    EfmFlashSeries0),
     81: Part("EFM32PG1B",  EfmFlashSeries1),
     83: Part("EFM32JG1B",  EfmFlashSeries1),
     85: Part("EFM32PG12B", EfmFlashSeries1),
     87: Part("EFM32JG12B", EfmFlashSeries1),
    100: Part("EFM32GG11B", EfmFlashSeries1GG),
    103: Part("EFM32TG11B", EfmFlashSeries1GG),
    106: Part("EFM32GG12B", EfmFlashSeries1GG),
    120: Part("EZR32LG",    EfmFlashSeries0),
    121: Part("EZR32WG",    EfmFlashSeries0),
    122: Part("EZR32HG",    EfmFlashSeries0),
}


PACKAGE_NAMES = {
    'J': "WLCSP",
    'L': "BGA",
    'M': "QFN",
    'Q': "QFP",
}


# DI (Device Information) block layout. Absolute addresses are
# DI_BASE + offset; one mem_read covers the whole probe.
DI_BASE          = 0x0FE081B0
DI_EUI48         = 0x028
DI_MEMINFO       = 0x034
DI_UNIQUE        = 0x040
DI_MSIZE         = 0x048
DI_PART          = 0x04C
DI_DEVINFOREV    = 0x050
DI_EMUTEMP       = 0x054
DI_END           = 0x058


class EfmTarget(CortexMTarget):
    """EFM32 / EFR32 family target — Cortex-M0+ / M3 / M4 with on-die
    flash programmed via the MSC peripheral."""


class EfmLoadable(Loadable):
    """EFM32 Loadable with CPU halt around flash ops.

    Halting before flash programming is required for reliability —
    MSC operations contend with CPU access to the flash bus. A
    successful program followed by `do_start` resets the chip out
    of halt and lets the new firmware run.
    """

    async def pre_program(self, *, do_erase, assume_clean):
        core = self.find_core()
        if core is not None:
            await core.halt()
        await super().pre_program(do_erase=do_erase, assume_clean=assume_clean)

    async def post_program(self, *, success, do_start):
        if do_start and success:
            core = self.find_core()
            if core is not None:
                await core.reset(stop=False)

    def find_core(self):
        target = self._parent
        if target is None:
            return None
        debuggables = target.children_of_class(Debuggable)
        if not debuggables or not debuggables[0].cores:
            return None
        return debuggables[0].cores[0]


@Target.register(Dp, precedence=500)
async def efm32_probe(dp):
    """Probe a DP for an EFM32 / EFR32 family chip.

    Walks Mem-APs under the DP, reads the DI block, and matches
    DI.PART.FAMILY against the known-part table. Unknown families
    or any access failure decline (`NoMatch`) so the generic
    Cortex-M target catches the device.
    """
    aps = dp.children_of_class(MemAp)
    if not aps:
        raise NoMatch("efm32_probe", "no MemAp under DP")
    for ap in aps:
        try:
            di = await ap.mem_read(DI_BASE, DI_END)
        except DpAccessFailure:
            continue
        family = di[DI_PART + 2]
        if family not in PARTS:
            continue
        return await _build_efm32_target(dp, ap, di)
    raise NoMatch("efm32_probe", "no EFM32 found behind DP")


async def _build_efm32_target(dp, ap, di):
    dev_number, family, _prod_ref = struct.unpack_from(
        "<HBB", di, DI_PART)
    flash_kb, ram_kb = struct.unpack_from("<HH", di, DI_MSIZE)
    _tempgrade, pkgtype, pincount, raw_page_size = struct.unpack_from(
        "<BBBB", di, DI_MEMINFO)
    pkgtype = chr(pkgtype)
    flash_size = flash_kb * 1024
    ram_size = ram_kb * 1024
    page_size = 1 << ((raw_page_size + 10) & 0xFF)

    part = PARTS[family]
    if part.flash_page_size is not None and page_size != part.flash_page_size:
        page_size = part.flash_page_size

    uid, = struct.unpack_from("<Q", di, DI_UNIQUE)

    name = f"{part.name}{dev_number}F{flash_kb}"
    if pincount:
        name += f"{pkgtype}{pincount}"
    # Suffix with the factory UID so multiple identically-modelled
    # chips parent at distinct paths under HwRoot.
    name += f"-{uid:016x}"

    rom_tables = ap.children_of_class(RomTable)
    rt = next((r for r in rom_tables if r.children_of_class(Scs)), None)
    if rt is None:
        raise NoMatch("efm32_probe", "no SCS under MemAp")

    target = EfmTarget(name)
    target.claim(dp, ap, rt)
    if pincount:
        target.logger.note("Package: %s%d",
                           PACKAGE_NAMES.get(pkgtype, pkgtype), pincount)
    if part.name.startswith("EFR"):
        mac = bytes(di[DI_EUI48 + 5:DI_EUI48 - 1:-1])
        target.logger.note(
            "EUI48 HWADDR: %s",
            ":".join(f"{b:02x}" for b in mac))

    debug = CortexMDebuggable.from_romtable(rt, ap)
    debug.memory_map.append(Ram("sram", 0x20000000, ram_size))
    debug.memory_map.append(Ram("di",   0x0FE00000, 0x10000))
    debug.memory_map.append(Ram("apb",  0x40000000, 0x100000))
    target.child_add(debug)

    memory = Memory(ap)
    memory.child_add(BusRam("flash", 0x00000000, flash_size, ap))
    memory.child_add(BusRam("sram",  0x20000000, ram_size, ap))
    memory.child_add(BusRam("di",    0x0FE00000, 0x10000, ap))
    memory.child_add(BusRam("apb",   0x40000000, 0x100000, ap))
    memory.child_add(BusRam("ppb",   0xE0000000, 0x100000, ap))
    target.child_add(memory)

    loadable = EfmLoadable("main")
    loadable.child_add(
        part.flash_class("code", 0x00000000, flash_size, ap,
                         page_size=page_size))
    if part.bootloader:
        loadable.child_add(
            part.flash_class("boot", 0x0FE10000, 16 * 1024, ap,
                             page_size=page_size))
    target.child_add(loadable)
    return target
