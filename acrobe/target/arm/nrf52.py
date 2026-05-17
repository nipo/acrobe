"""Nordic Semiconductor nRF52 family target.

nRF52 flash programming runs on-target through the NVMC stubs from
crobe's `firmware/flash/stubs/arm/nrf51.c` — the NVMC peripheral
layout is identical between nRF51 and nRF52, and Cortex-M0 Thumb
code runs on the M4 unchanged. Each stub takes
`(addr, size_or_src, page_or_bytes)` in r0..r2 and loops on-target,
keeping per-word NVMC pokes off the SWD bus.

Mass-erase via CTRL-AP stays on its dedicated AP path (one-shot,
faster + works through APPROTECT). UICR erase keeps the direct
NVMC.ERASEUICR write (also one-shot).

Detection runs against the AHB-AP under each DP: read
FICR.INFO.PART (0x10000100) and match against the known
part-number table. Unknown parts decline so the generic
Cortex-M target picks them up.
"""

from __future__ import annotations

import asyncio

from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.dp import Dp, DpAccessFailure
from ...component.arm.memory import BusRam
from ...component.arm.mem_ap import MemAp
from ...component.nordic.ctrl_ap import CtrlAp
from ...db import NoMatch
from ..debuggable import Debuggable
from ..loadable import Loadable
from ..memory import Memory
from ..puppet import ArmMPuppet, PagedPuppetWriter
from ..region import Flash, Ram
from ..target import Target
from .cortex_m import CortexMDebuggable, CortexMTarget


# Stub blobs from crobe firmware/flash/stubs/arm/nrf51.c. Calling
# convention (AAPCS, r0..r2 = args):
#   void flash_erase(uintptr_t addr, size_t size, size_t page_size);
#   void flash_write(uintptr_t dst, const void *src, size_t bytes);
# Drives NVMC at 0x4001E000 — identical on nRF51 and nRF52.
NRF_STUBS = {
    "flash_erase": b'\xf7\xb5\x02%S\x1eA\x18\x0fL\x18@\x0fK\x80&\xe5P\xa1%\xed\x00\x01\'\xacF\x01\x93\xf6\x00\x80%\xed\x00\x88B\x07\xd2\xa5Y=B\xfc\xd0cF\x06M\xe8P\x80\x18\xf3\xe7\x01"aY\x11B\xfc\xd0\x00!\x01J\x01\x9b\xd1P\xf7\xbd\x00\xe0\x01@\x04\x05\x00\x00',
    "flash_write": b'\xf0\xb5\x01%\x80\'\rK\rL\x92\x08\x92\x00\x1dQ\x12\x18\xff\x00\t\x1a\x80&\xf6\x00\x90B\x05\xd0\xdeY.B\xfc\xd0\x0eX@\xc0\xf5\xe7\x01"\x99Y\x11B\xfc\xd0\x00"\x01K\x1aQ\xf0\xbd\xc0F\x00\xe0\x01@\x04\x05\x00\x00',
}


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
FICR_DEVICEADDR0  = FICR_BASE + 0x0A4
FICR_DEVICEADDR1  = FICR_BASE + 0x0A8
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
    """nRF52 flash region driven by on-target stub code (Puppet).

    Reads pass through Mem-AP `mem_read` (flash is memory-mapped at
    the region's base). Erase and write call into the `flash_erase`
    / `flash_write` stubs running on the target Cortex-M; the host
    only pushes input bytes into RAM and waits for the stub to
    return. The stub manages NVMC.CONFIG / NVMC.READY entirely
    target-side, keeping per-word pokes off the SWD bus."""

    POLL_PERIOD = 0.001

    # Per-call timeout caps. Stubs that lock up don't get unbounded
    # time — `Puppet.wait` will force-halt the core and raise.
    STUB_ERASE_TIMEOUT = 30.0
    STUB_WRITE_TIMEOUT = 5.0

    # Cap each Mem-AP `mem_read` issued during readback / verify so
    # the J-Link OB doesn't latch a sticky error after ~80 KiB of
    # continuous AP reads in one batch. Real J-Link firmware
    # doesn't need this but it costs nothing to keep on either.
    READ_CHUNK = 32 * 1024

    def __init__(self, name, address, size, mem_ap, puppet, *,
                 page_size: int):
        super().__init__(name, address, size,
                         write_page_size=page_size,
                         erase_page_sizes=[page_size])
        self.mem_ap = mem_ap
        self.puppet = puppet
        # Pipelined writer set up by `plan_update` for the duration of
        # one region update; per-page `write()` calls thread chunks
        # into it so host upload of page N+1 overlaps with target burn
        # of page N. None means standalone write — `write()` builds a
        # one-shot writer on demand.
        self.__writer: PagedPuppetWriter | None = None

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
                f"NvmcFlash erase must be page-aligned "
                f"(page={self.write_page_size:#x})")
        stub = self.puppet.stub(NRF_STUBS["flash_erase"], name="nvmc_erase")
        try:
            n_pages = size // self.write_page_size
            # Single-page erases come from Loadable.write → plan_update
            # and run inside the per-region "program" bar; no point
            # opening a 1/1 bar of our own. Multi-page erases come
            # from Loadable.erase_all — show a bar so the operator
            # knows we're alive.
            with self.progress("erase", n_pages, "pages") as bar:
                await stub.call(self.address + offset, size,
                                self.write_page_size,
                                timeout=self.STUB_ERASE_TIMEOUT)
                bar.advance(n_pages)
        finally:
            stub.cleanup()
        if offset == 0 and size == self.size:
            self.is_blank = True

    async def plan_update(self, region_map):
        """Same chunk schedule as `Flash.plan_update`, but with a
        long-lived `PagedPuppetWriter` set up around the yield loop
        so per-page `write()` calls pipeline across iterations."""
        if not self.is_blank:
            await self._erase_for(region_map)
        paged = region_map.paged(self.write_page_size,
                                 fill=bytes([self.erased_value]))
        page = self.write_page_size
        stub = self.puppet.stub(NRF_STUBS["flash_write"], name="nvmc_write")
        try:
            async with PagedPuppetWriter(
                    stub, page,
                    timeout=self.STUB_WRITE_TIMEOUT) as writer:
                self.__writer = writer
                try:
                    for addr, data in paged:
                        offset = addr - self.address
                        for o in range(0, len(data), page):
                            yield offset + o, data[o:o + page]
                finally:
                    self.__writer = None
        finally:
            stub.cleanup()

    async def write(self, offset, data):
        if offset % 4 or len(data) % 4:
            raise ValueError("NvmcFlash write must be word-aligned")
        if self.__writer is not None:
            await self.__writer.write(self.address + offset, data)
            return
        # Standalone call (not driven by `plan_update`) — one-shot writer.
        stub = self.puppet.stub(NRF_STUBS["flash_write"], name="nvmc_write")
        try:
            page = self.write_page_size
            async with PagedPuppetWriter(
                    stub, page,
                    timeout=self.STUB_WRITE_TIMEOUT) as w:
                for chunk_off in range(0, len(data), page):
                    chunk = data[chunk_off:chunk_off + page]
                    await w.write(self.address + offset + chunk_off, chunk)
        finally:
            stub.cleanup()

    async def set_config(self, value: int):
        """Write NVMC.CONFIG and wait for the controller to settle.

        Kept for callers (UicrFlash, future provisioning flows) that
        manage NVMC state at a higher level than the per-call stubs.
        """
        await self.mem_ap.write32(NVMC_CONFIG, value)
        await self.wait_ready()

    async def wait_ready(self, timeout: float = 5.0):
        """Poll NVMC.READY until set or `timeout` seconds elapse."""
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


class UicrFlash(NvmcFlash):
    """UICR (User Information Configuration Registers) region.

    Writes go through the same flash_write stub as main flash. Erase
    is all-or-nothing via NVMC.ERASEUICR — kept on the MMIO path
    because it's a single one-shot register poke (stub overhead
    would dwarf it). Partial erase is unsupported."""

    async def erase(self, offset, size):
        if offset != 0 or size != self.size:
            raise ValueError(
                "UicrFlash erase is all-or-nothing; "
                "offset must be 0 and size must equal region size")
        await self.set_config(NVMC_CONFIG_EEN)
        try:
            await self.mem_ap.write32(NVMC_ERASEUICR, 1)
            await self.wait_ready()
        finally:
            await self.set_config(NVMC_CONFIG_REN)
        self.is_blank = True


class Nrf52Loadable(Loadable):
    """nRF52 Loadable with CPU halt around flash ops and an
    optional CTRL-AP mass-erase shortcut.

    Halting before flash programming is required for reliability —
    NVMC operations contend with CPU access to flash. Without it,
    the first attempt against a chip running user code typically
    fails until something else (mass-erase + reset) clears the
    flash bus."""

    def __init__(self, name: str = "main", *, ctrl_ap: CtrlAp | None = None,
                 locked: bool = False):
        super().__init__(name)
        self.ctrl_ap = ctrl_ap
        # `locked` reflects APPROTECT state at construction time —
        # debug + flash R/W are unreachable; only erase_all (via
        # CTRL-AP) does anything useful. Set when nrf52_probe finds
        # the chip locked.
        self.locked = locked

    async def pre_program(self, *, do_erase, assume_clean):
        if self.locked:
            if not do_erase:
                raise RuntimeError(
                    "APPROTECT is enabled on this nRF52 — cannot program. "
                    "Re-run with --erase (or use `chip erase-all`) to "
                    "mass-erase via CTRL-AP; that clears flash + UICR + "
                    "APPROTECT in one go.")
            # CTRL-AP ERASEALL also unlocks. After this call the chip
            # is reachable, but our cached Target instance still
            # carries `locked=True` and has no regions — user re-runs
            # without --erase to actually flash.
            await self.erase_all()
            self.logger.note(
                "APPROTECT cleared. Re-run `acrobe chip ... program "
                "<file>` (no --erase) to flash now that the chip is "
                "accessible.")
            return
        core = self.__core()
        if core is not None:
            await core.halt()
        await super().pre_program(do_erase=do_erase, assume_clean=assume_clean)

    async def post_program(self, *, success, do_start):
        if do_start and success:
            core = self.__core()
            if core is not None:
                await core.reset(stop=False)

    async def erase_all(self):
        """Prefer CTRL-AP ERASEALL when available — single 175ms
        operation that clears flash + UICR + APPROTECT in one go.

        Falls back to per-region per-page NVMC erase if no CTRL-AP
        is wired."""
        if self.ctrl_ap is None:
            await super().erase_all()
            return
        await self.ctrl_ap.erase_all()
        for f in self.children_of_class(Flash):
            f.is_blank = True
        # CTRL-AP ERASEALL leaves the CPU held in reset on some
        # nRF52 revisions; release and bring debug back up.
        await self.ctrl_ap.release_reset()
        core = self.__core()
        if core is not None:
            await core.reset(stop=True)

    def __core(self):
        target = self._parent
        if target is None:
            return None
        debuggables = target.children_of_class(Debuggable)
        if not debuggables or not debuggables[0].cores:
            return None
        return debuggables[0].cores[0]


class Nrf52Target(CortexMTarget):
    """nRF52 family target — Cortex-M4 (M4F on -840/-833) with NVMC
    flash programming."""


@Target.register(Dp, precedence=500)
async def nrf52_probe(dp):
    """Probe a DP for an nRF52-family chip.

    Two paths:

    1. CTRL-AP reports APPROTECT enabled → build a *locked* Target
       that exposes only erase-all via CTRL-AP. Avoids the silent
       failure of an FICR read on a locked chip.

    2. APPROTECT clear (or no CTRL-AP at all): walk Mem-AP children,
       read FICR.INFO.PART, match against the known part table.
       Declines (NoMatch) for unknown parts so the generic Cortex-M
       target catches them.
    """
    ctrl_aps = dp.children_of_class(CtrlAp)
    ctrl_ap = ctrl_aps[0] if ctrl_aps else None

    if ctrl_ap is not None:
        try:
            locked = await ctrl_ap.is_protected()
        except DpAccessFailure:
            locked = False
        if locked:
            return _build_locked_target(dp, ctrl_ap)

    aps = dp.children_of_class(MemAp)
    if not aps:
        raise NoMatch("nrf52_probe", "no MemAp under DP")
    for ap in aps:
        try:
            part = await ap.read32(FICR_INFO_PART)
        except DpAccessFailure:
            continue
        if part not in NRF52_PARTS:
            continue
        return await _build_nrf52_target(dp, ap, part, ctrl_ap)
    raise NoMatch("nrf52_probe", "no nRF52 found behind DP")


def _build_locked_target(dp, ctrl_ap):
    """Build a partial Target for an APPROTECT-locked nRF52.

    No Debuggable (debug access denied). No Flash regions (FICR
    read would fail). Loadable carries only the CTRL-AP path so
    the user can mass-erase via `chip program --erase` or
    `chip erase-all`.
    """
    target = Nrf52Target("nRF52 (APPROTECT locked)")
    target.claim(dp, ctrl_ap)
    target.logger.warning(
        "APPROTECT is enabled — debug + flash read/write are blocked. "
        "Run `acrobe chip ... erase-all` (or `chip ... program --erase "
        "<file>`) to mass-erase via CTRL-AP. This clears flash + UICR "
        "+ APPROTECT in one operation.")
    loadable = Nrf52Loadable("main", ctrl_ap=ctrl_ap, locked=True)
    target.child_add(loadable)
    return target


async def _build_nrf52_target(dp, ap, part, ctrl_ap):
    page_size = await ap.read32(FICR_CODEPAGESIZE)
    page_count = await ap.read32(FICR_CODESIZE)
    flash_size = page_size * page_count
    ram_kb = await ap.read32(FICR_INFO_RAM)
    addr_lo = await ap.read32(FICR_DEVICEADDR0)
    addr_hi = await ap.read32(FICR_DEVICEADDR1)
    bdaddr = ((addr_hi & 0xFFFF) << 32) | addr_lo

    rom_tables = ap.children_of_class(RomTable)
    rt = next((r for r in rom_tables if r.children_of_class(Scs)), None)
    if rt is None:
        raise NoMatch("nrf52_probe", "no SCS under MemAp")

    # Suffix with the factory BLE address so multiple identically-
    # modelled chips parent at distinct paths under HwRoot.
    name = f"{NRF52_PARTS[part]}-{bdaddr:012x}"
    target = Nrf52Target(name)
    target.claim(dp, ap, rt)
    if ctrl_ap is not None:
        target.claim(ctrl_ap)

    debug = CortexMDebuggable.from_romtable(rt, ap)
    # Tell GDB which ranges are accessible — without these, its
    # memory-map clamping rejects `x/...` of any address outside the
    # declared map before even sending an `m` packet.
    #
    # The Cortex-M PPB (0xE0000000) is already in `memory_map` by
    # default. We add the nRF52-specific ranges:
    #   FICR @ 0x10000000 (4 KiB)       — factory info, read-only
    #   SRAM @ 0x20000000 (ram_kb KiB)  — main RAM
    #   APB peripherals @ 0x40000000-0x40080000
    #   AHB peripherals @ 0x50000000-0x50080000
    debug.memory_map.append(Ram("ficr", 0x10000000, 0x1000))
    debug.memory_map.append(Ram("sram", 0x20000000, ram_kb * 1024))
    debug.memory_map.append(Ram("apb",  0x40000000, 0x80000))
    debug.memory_map.append(Ram("ahb",  0x50000000, 0x80000))
    target.child_add(debug)

    # Memory view — same ranges, but as functional BusRam children
    # backed by the AHB-AP. Anchor for memory-aware clients (RTT
    # under sram, future peripheral drivers under apb / ahb).
    sram = BusRam("sram", 0x20000000, ram_kb * 1024, ap)
    memory = Memory(ap)
    memory.child_add(BusRam("flash", 0x00000000, flash_size, ap))
    memory.child_add(BusRam("ficr",  0x10000000, 0x1000, ap))
    memory.child_add(sram)
    memory.child_add(BusRam("apb",   0x40000000, 0x80000, ap))
    memory.child_add(BusRam("ahb",   0x50000000, 0x80000, ap))
    memory.child_add(BusRam("ppb",   0xE0000000, 0x100000, ap))
    target.child_add(memory)

    puppet = ArmMPuppet("puppet", debug.cores[0], sram, ap)
    target.child_add(puppet)

    loadable = Nrf52Loadable("main", ctrl_ap=ctrl_ap)
    loadable.child_add(
        NvmcFlash("code", 0x00000000, flash_size, ap, puppet,
                  page_size=page_size))
    loadable.child_add(
        UicrFlash("uicr", UICR_BASE, page_size, ap, puppet,
                  page_size=page_size))
    target.child_add(loadable)
    return target
