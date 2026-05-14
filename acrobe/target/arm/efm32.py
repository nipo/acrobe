"""Silicon Labs (Energy Micro) EFM32 / EFR32 family target.

EFM32 flash programming runs on-target through stub code driven by
the Puppet substrate (`acrobe.target.puppet.ArmMPuppet`). The
~70-byte `flash_erase` / `flash_write` stubs in `STUBS` are the
ones crobe ships in `firmware/flash/stubs/arm/efm32.c`, compiled
per series with the appropriate MSC base address. Each stub takes
(addr, size_or_src, page_or_bytes) in r0..r2 and loops on-target,
keeping per-word MSC pokes off the SWD bus.

Mass-erase still goes through MMIO (one-shot path via
`MSC_WRITECMD_ERASEMAIN0`/`ERASEMAIN1`).

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
from ..puppet import ArmMPuppet
from ..region import Flash, Ram
from ..target import Target
from .cortex_m import CortexMDebuggable, CortexMTarget


# Stub blobs from crobe firmware/flash/stubs/arm/efm32.c, compiled
# per series. Calling convention (AAPCS, r0..r3 = args, r0 = ret):
#   void flash_erase(uintptr_t addr, size_t size, size_t page_size);
#   void flash_write(uintptr_t dst, const void *src, size_t bytes);
# bytes() is RAM that the host pre-populates via mem_write.
STUBS = {
    "series0g": {
        "flash_erase": b'p\xb5SB\rL\x01D\x18@\xe3i\x13\xf0\x01\x0f\nK\xfa\xd1A\xf6q4\xdcc\x01$\x9c`\x02%\x88B\x07\xd2\x18a\xdc`\x10D\xdd`\xdei\xf6\x07\xfc\xd4\xf5\xe7\x00"\x9a`\xdacp\xbd\x00\x00\x0c@',
        "flash_write": b'p\xb5\x10L\x92\x08\xe3i\x13\xf0\x01\x0f\rK\xfa\xd1A\xf6q4\xdcc\x01$\x9c`\x01\xeb\x82\x02@\x1a\x08%\x91B\n\xd0F\x18\x1ea\xdc`Q\xf8\x04k\x9ea\xdd`\xdei\xf6\x07\xfc\xd4\xf2\xe7\x00"\x9a`\xdacp\xbd\x00\x00\x0c@',
    },
    "series0": {
        "flash_erase": b'\xf0\xb5\x01%SB\rOA\x18\x18@\xfei\x01$\nK.B\xfa\xd1\nM\xddc\x02%\x9c`\x88B\x07\xd2\x18a\xdc`\x80\x18\xdd`\xdei&B\xfc\xd1\xf5\xe7\x00"\x9a`\xdac\xf0\xbd\xc0F\x00\x00\x0c@q\x1b\x00\x00',
        "flash_write": b'\xf0\xb5\x01%\x0eO\x92\x08\xfei\x01$\x0cK.B\xfa\xd1\x0cM\x92\x00\xddc\x9c`\x18a\x08 \xdc`\x8a\x18\x91B\x07\xd0\rh\x9da\xd8`\xddi%B\xfc\xd1\x041\xf5\xe7\x00"\x9a`\xdac\xf0\xbd\xc0F\x00\x00\x0c@q\x1b\x00\x00',
    },
    "series1": {
        "flash_erase": b'\xf0\xb5\x01%SB\rOA\x18\x18@\xfei\x01$\nK.B\xfa\xd1\nM\x1dd\x02%\x9c`\x88B\x07\xd2\x18a\xdc`\x80\x18\xdd`\xdei&B\xfc\xd1\xf5\xe7\x00"\x9a`\x1ad\xf0\xbd\xc0F\x00\x00\x0e@q\x1b\x00\x00',
        "flash_write": b'\xf0\xb5\x01%\x0eO\x92\x08\xfei\x01$\x0cK.B\xfa\xd1\x0cM\x92\x00\x1dd\x9c`\x18a\x08 \xdc`\x8a\x18\x91B\x07\xd0\rh\x9da\xd8`\xddi%B\xfc\xd1\x041\xf5\xe7\x00"\x9a`\x1ad\xf0\xbd\xc0F\x00\x00\x0e@q\x1b\x00\x00',
    },
    "series1gg": {
        "flash_erase": b'\xf0\xb5\x80\'\x01%SBA\x18\xff\x05\x18@\x80#\xfei\x01$\xdb\x05.B\xf9\xd1\tM\x1dd\x02%\x9c`\x88B\x07\xd2\x18a\xdc`\x80\x18\xdd`\xdei&B\xfc\xd1\xf5\xe7\x00"\x9a`\x1ad\xf0\xbd\xc0Fq\x1b\x00\x00',
        "flash_write": b'\xf0\xb5\x80\'\x01%\x92\x08\xff\x05\x80#\xfei\x01$\xdb\x05.B\xf9\xd1\x0bM\x92\x00\x1dd\x9c`\x18a\x08 \xdc`\x8a\x18\x91B\x07\xd0\rh\x9da\xd8`\xddi%B\xfc\xd1\x041\xf5\xe7\x00"\x9a`\x1ad\xf0\xbd\xc0Fq\x1b\x00\x00',
    },
}


class Part:
    """One row of the EFM32 family table."""

    def __init__(self, name, flash_class,
                 flash_page_size=None, bootloader=False):
        self.name = name
        self.flash_class = flash_class
        self.flash_page_size = flash_page_size
        self.bootloader = bootloader


class EfmFlash(Flash):
    """EFM32 flash region driven by on-target stub code (Puppet).

    Reads pass through Mem-AP `mem_read` (flash is memory-mapped at
    the region base). Erase and write call into the per-series
    `flash_erase` / `flash_write` stubs running on the target CPU;
    the host only pushes input bytes into RAM and waits for the
    stub to return. Full-region erase shortcuts through `mass_erase`
    via MMIO (one-shot ERASEMAIN command).

    Subclasses set `MSC` / `MSC_LOCK` (used by `mass_erase`) and
    `STUBS` (the per-series compiled blob set).
    """

    MSC = None
    MSC_LOCK = None
    STUBS: dict = {}

    MSC_LOCK_MAGIC = 0x1B71
    MSC_MASSLOCK_MAGIC = 0x631A

    MSC_WRITECTRL = 0x008
    MSC_WRITECMD  = 0x00C
    MSC_STATUS    = 0x01C
    MSC_MASSLOCK  = 0x054

    MSC_WRITECTRL_WREN      = 1 << 0
    MSC_WRITECMD_ERASEMAIN0 = 1 << 8
    MSC_WRITECMD_ERASEMAIN1 = 1 << 9

    MSC_STATUS_BUSY = 1 << 0

    POLL_PERIOD = 0.001

    # Per-call timeout caps. Stubs that lock up don't get unbounded
    # time — `Puppet.wait` will force-halt the core and raise.
    STUB_ERASE_TIMEOUT = 30.0
    STUB_WRITE_TIMEOUT = 5.0

    # Cap each Mem-AP `mem_read` issued during readback / verify.
    # The Silicon Labs J-Link OB latches an error after ~80 KiB of
    # continuous AP reads in one engine batch; smaller per-call
    # reads give the SwDp/Mem-AP pipeline natural break points.
    READ_CHUNK = 32 * 1024

    # Mass-erase fast path via MSC_WRITECMD_ERASEMAIN0/1. Overridden
    # to False on Series 0G — that variant has no ERASEMAIN command
    # and must fall back to per-page erase.
    has_mass_erase = True

    def __init__(self, name, address, size, mem_ap, puppet, *, page_size):
        super().__init__(name, address, size,
                         write_page_size=page_size,
                         erase_page_sizes=[page_size])
        self.mem_ap = mem_ap
        self.puppet = puppet
        self.write_buffer = None

    async def read(self, offset, size):
        out = bytearray()
        base = self.address + offset
        for chunk_off in range(0, size, self.READ_CHUNK):
            n = min(self.READ_CHUNK, size - chunk_off)
            out += await self.mem_ap.mem_read(base + chunk_off, n)
        return bytes(out)

    async def erase(self, offset, size):
        if offset % self.write_page_size or size % self.write_page_size:
            raise ValueError(
                f"EfmFlash erase must be page-aligned "
                f"(page={self.write_page_size:#x})")
        full_region = (offset == 0 and size == self.size)
        if full_region and self.has_mass_erase:
            await self.mass_erase()
            return
        stub = self.puppet.stub(self.STUBS["flash_erase"], name="efm_erase")
        try:
            with self.progress("erase",
                               size // self.write_page_size, "pages") as bar:
                await stub.call(self.address + offset, size,
                                self.write_page_size,
                                timeout=self.STUB_ERASE_TIMEOUT)
                bar.advance(size // self.write_page_size)
        finally:
            stub.cleanup()
        if full_region:
            self.is_blank = True

    async def write(self, offset, data):
        if offset % 4 or len(data) % 4:
            raise ValueError("EfmFlash write must be word-aligned")
        stub = self.puppet.stub(self.STUBS["flash_write"], name="efm_write")
        self.write_buffer = self.puppet.allocate(self.write_page_size,
                                                 align=4)
        try:
            page = self.write_page_size
            for chunk_off in range(0, len(data), page):
                chunk = data[chunk_off:chunk_off + page]
                await self.write_buffer.write(chunk)
                await stub.call(self.address + offset + chunk_off,
                                self.write_buffer.address,
                                len(chunk),
                                timeout=self.STUB_WRITE_TIMEOUT)
        finally:
            self.puppet.unallocate(self.write_buffer)
            self.write_buffer = None
            stub.cleanup()

    async def mass_erase(self):
        """Chip-wide flash erase via MSC_WRITECMD_ERASEMAIN0 (+ MAIN1
        on parts with >= 512 KiB).

        Stays on the MMIO path — it's a one-shot transaction, the
        stub overhead would be larger than the savings.
        Requires both MSC_LOCK and MSC_MASSLOCK to be unlocked.
        Series 0G subclasses this away (no ERASEMAIN command on
        that variant).
        """
        await self.unlock()
        await self.mass_unlock()
        try:
            await self.mem_ap.write32(self.MSC + self.MSC_WRITECTRL,
                                      self.MSC_WRITECTRL_WREN)
            await self.wait_idle()
            await self.mem_ap.write32(self.MSC + self.MSC_WRITECMD,
                                      self.MSC_WRITECMD_ERASEMAIN0)
            await self.wait_idle(timeout=20.0)
            if self.size >= (1 << 19):
                await self.mem_ap.write32(self.MSC + self.MSC_WRITECMD,
                                          self.MSC_WRITECMD_ERASEMAIN1)
                await self.wait_idle(timeout=20.0)
        finally:
            await self.mass_lock()
            await self.lock()
        self.is_blank = True

    async def unlock(self):
        await self.mem_ap.write32(self.MSC + self.MSC_LOCK,
                                  self.MSC_LOCK_MAGIC)

    async def lock(self):
        await self.mem_ap.write32(self.MSC + self.MSC_LOCK, 0)

    async def mass_unlock(self):
        await self.mem_ap.write32(self.MSC + self.MSC_MASSLOCK,
                                  self.MSC_MASSLOCK_MAGIC)

    async def mass_lock(self):
        await self.mem_ap.write32(self.MSC + self.MSC_MASSLOCK, 0)

    async def wait_idle(self, timeout: float = 5.0):
        """Poll MSC_STATUS until BUSY clears or `timeout` elapses."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            status = await self.mem_ap.read32(self.MSC + self.MSC_STATUS)
            if not (status & self.MSC_STATUS_BUSY):
                return
            if loop.time() > deadline:
                raise TimeoutError(
                    f"MSC busy after {timeout}s (status={status:#x})")
            await asyncio.sleep(self.POLL_PERIOD)


class EfmFlashSeries0(EfmFlash):
    MSC = 0x400C0000
    MSC_LOCK = 0x3C
    STUBS = STUBS["series0"]


class EfmFlashSeries0G(EfmFlashSeries0):
    """EFM32G — Series 0 without MSC mass-erase support.

    Full-region erase falls back to per-page through the inherited
    `erase()` path because `has_mass_erase` is False.
    """

    has_mass_erase = False
    STUBS = STUBS["series0g"]


class EfmFlashSeries1(EfmFlash):
    MSC = 0x400E0000
    MSC_LOCK = 0x40
    STUBS = STUBS["series1"]


class EfmFlashSeries1GG(EfmFlash):
    MSC = 0x40000000
    MSC_LOCK = 0x40
    STUBS = STUBS["series1gg"]


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

    sram = BusRam("sram",  0x20000000, ram_size, ap)
    memory = Memory(ap)
    memory.child_add(BusRam("flash", 0x00000000, flash_size, ap))
    memory.child_add(sram)
    memory.child_add(BusRam("di",    0x0FE00000, 0x10000, ap))
    memory.child_add(BusRam("apb",   0x40000000, 0x100000, ap))
    memory.child_add(BusRam("ppb",   0xE0000000, 0x100000, ap))
    target.child_add(memory)

    puppet = ArmMPuppet("puppet", debug.cores[0], sram, ap)
    target.child_add(puppet)

    loadable = EfmLoadable("main")
    loadable.child_add(
        part.flash_class("code", 0x00000000, flash_size, ap, puppet,
                         page_size=page_size))
    if part.bootloader:
        loadable.child_add(
            part.flash_class("boot", 0x0FE10000, 16 * 1024, ap, puppet,
                             page_size=page_size))
    target.child_add(loadable)
    return target
