"""Raspberry Pi RP2350 SWD target.

Unlike the RP2040 (one DP per core plus a rescue DP, see
``rp2040_swd``), the RP2350 has a single multidrop-capable SW-DP
(DPv3 / ADIv6) with everything behind it as APs in the DP's
address space (RP2350 datasheet §3.5; AP layout as used by
Raspberry Pi's OpenOCD ``target/rp2350.cfg``):

    0x02000  Cortex-M33 core 0 (AHB5-AP)
    0x04000  Cortex-M33 core 1 (AHB5-AP)
    0x0A000  RISC-V Debug Module (APB-AP), both Hazard3 cores
    0x80000  RP-AP (rescue reset, debug key)

Rescue is done through RP-AP CTRL[31], not a rescue DP.

There is no sibling-DP gathering to do, so this module hooks the
generic :func:`arm_soc_probe` path via :data:`ArmSocTarget.db`,
keyed on the DP's TARGETID (``0x00040927``; the match ignores
bits 31:28, so any POWMAN.DBGCONFIG.DP_INSTID selects it). By the
time it runs, the BASEPTR0 ROM-table walk has instantiated a
:class:`MemApV2` per Arm core AP with that core's ROM Table and
SCS beneath it; the probe builds one :class:`CortexMDebuggable`
per core from those.

Programming goes through :class:`Rp2350Loadable`, which holds an
SRAM and a QSPI flash region and picks one per image: pico-sdk
``no_flash`` images are loaded into SRAM and started there; flash
images are written to the external QSPI flash through the bootrom's
flash API (:class:`Rp2350Bootrom`) and booted by the bootrom.
RP2350A/B have no internal flash, so on boards without a QSPI flash
only SRAM images can be used.
"""

from __future__ import annotations

import asyncio
import struct

from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.memory import BusRam
from ...component.arm.mem_ap import MemApV2
from ...db import NoMatch
from ...part_id import PartId
from ..debuggable import CoreState
from ...memory_map import MemoryMap
from ..loadable import Loadable, coerce_memory_map
from ..puppet import ArmMPuppet
from ..region import Flash, Ram
from .cortex_m import CortexMDebuggable
from .soc import ArmSocTarget


# DP TARGETID: TPARTNO 0x0040, designer 0x493 (Raspberry Pi). Not
# the same number as SYSINFO.CHIP_ID's part (0x0004).
RP2350_TARGETID = PartId.from_idcode(0x00040927)

# AP base address -> Cortex-M33 core index.
RP2350_CORE_APS = {
    0x2000: 0,
    0x4000: 1,
}


# Bootrom, and SRAM0..9: 8 x 64 KiB striped banks plus the two
# 4 KiB SCRATCH_X / SCRATCH_Y banks right above them.
ROM_BASE  = 0x00000000
ROM_SIZE  = 0x00008000
SRAM_BASE = 0x20000000
SRAM_SIZE = 0x00082000
SRAM_END  = SRAM_BASE + SRAM_SIZE

# XIP window of QSPI chip select 0, and its uncached, non-allocating
# alias (reads don't go through, or disturb, the XIP cache).
XIP_BASE         = 0x10000000
XIP_NOCACHE_BASE = 0x14000000
XIP_WINDOW       = 0x01000000

# QMI direct mode: serial access to the flash, used for its JEDEC ID.
QMI_DIRECT_CSR = 0x400D0000
QMI_DIRECT_TX  = 0x400D0004
QMI_DIRECT_RX  = 0x400D0008
QMI_CSR_EN          = 1 << 0
QMI_CSR_BUSY        = 1 << 1
QMI_CSR_ASSERT_CS0N = 1 << 2

# Watchdog SCRATCH4..7: boot vector the bootrom honours on its next run
# (RP2350 datasheet §5.3 "Check Watchdog Vector"; the same encoding as
# pico-sdk's watchdog_reboot(pc, sp, ...)).
WATCHDOG_SCRATCH4 = 0x400D801C
BOOT_VECTOR_MAGIC = 0xB007C0D3

SCB_VTOR = 0xE000ED08

# Bootrom API (datasheet §5.4): the halfword at 0x16 points to
# rom_table_lookup(code, mask) on Arm.
BOOTROM_TABLE_LOOKUP = 0x16
RT_FLAG_FUNC_ARM_SEC = 0x0004
BOOTROM_FUNCS = {
    "connect_internal_flash": "IF",
    "flash_exit_xip":         "EX",
    "flash_range_erase":      "RE",
    "flash_range_program":    "RP",
    "flash_flush_cache":      "FC",
    "flash_enter_cmd_xip":    "CX",
}

# SRAM the bootrom helper takes over: a halt stub, its stack, then the
# puppet's trampoline, stack and data buffer. The SCRATCH_X/Y banks at
# the top of SRAM hold pico-sdk's core stacks, which needn't survive a
# restart, so an SRAM image loaded below them stays intact.
BOOTROM_RAM_BASE = 0x20080000
BOOTROM_RAM_SIZE = 0x00002000


def _sram_vector(sp: int, pc: int) -> bool:
    """Whether (sp, pc) from a vector table is a no_flash image's."""
    return (SRAM_BASE < sp <= SRAM_END and sp % 8 == 0
            and pc & 1 == 1 and SRAM_BASE <= pc < SRAM_END)


async def _boot_through_vector(debug, sp: int, pc: int,
                               vtor: int | None = None) -> None:
    """Reset core 0 and have the bootrom call ``pc`` with ``sp``.

    Core 0 is reset with reset catch (SYSRESETREQ, which on RP2350
    resets only the requesting core), halting it at the bootrom's
    reset entry with SAU, MPU, NVIC and fault status at reset values.
    The watchdog boot vector is set (and VTOR, if given), then the
    core resumed: the bootrom seeds the redundancy coprocessor and
    calls ``pc``, before the point where, with no bootable image, it
    would fall into BOOTSEL and zero SRAM."""
    core = debug.cores[0]
    await core.halt()
    await core.reset(stop=True)
    if await core.state() != CoreState.HALT:
        dhcsr = await core.scs.read_dhcsr()
        raise RuntimeError(
            f"core 0 did not halt on reset catch (DHCSR 0x{dhcsr:08x})")
    if vtor is not None:
        await debug.mem_write(SCB_VTOR, struct.pack("<I", vtor))
    await debug.mem_write(WATCHDOG_SCRATCH4, struct.pack(
        "<IIII", BOOT_VECTOR_MAGIC,
        pc ^ (-BOOT_VECTOR_MAGIC & 0xFFFFFFFF), sp, pc))
    await core.resume()


class _BootromPuppet(ArmMPuppet):
    # The bootrom's flash functions need more than the default.
    STACK_SIZE = 4096


class Rp2350Bootrom:
    """Calls RP2350 bootrom API functions on core 0.

    The bootrom functions rely on state the bootrom sets up early in
    boot: the redundancy coprocessor (CP7) enabled and its canaries
    seeded. A reset-caught core has neither, so :meth:`open` lets the
    bootrom do it: a ``bkpt`` stub goes into SRAM, and core 0 is
    booted through the watchdog boot vector into it. The core halts
    there in a bootrom-initialised state, and functions are then run
    through an :class:`ArmMPuppet` over the rest of the SRAM block.
    Their addresses are looked up with ``rom_table_lookup`` on every
    open, as they differ between bootrom versions.

    Opening takes over core 0 and :data:`BOOTROM_RAM_SIZE` bytes of
    SRAM; anything that resets or runs core 0 afterwards must
    :meth:`close` first."""

    STUB = struct.pack("<HH", 0xBE00, 0xE7FE)  # bkpt #0; b .
    # Stack for the bootrom's call into the stub, below the puppet.
    STUB_STACK = 0x200

    # How long the bootrom gets to reach the stub.
    OPEN_TIMEOUT = 1.0

    def __init__(self, debuggable, base: int = BOOTROM_RAM_BASE,
                 size: int = BOOTROM_RAM_SIZE):
        self.debuggable = debuggable
        self.base = base
        self.size = size
        self.__puppet: ArmMPuppet | None = None
        self.__funcs: dict[str, int] = {}
        # Bumped on every open, so users of the puppet's allocations
        # can tell they're stale.
        self.generation = 0

    @property
    def is_open(self) -> bool:
        return self.__puppet is not None

    async def open(self) -> None:
        if self.__puppet is not None:
            return
        debug = self.debuggable
        core = debug.cores[0]
        await core.halt()
        await debug.mem_write(self.base, self.STUB)
        await _boot_through_vector(debug, sp=self.base + self.STUB_STACK,
                                   pc=self.base | 1)

        pc = core.lookup_register("pc")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.OPEN_TIMEOUT
        while await core.state() != CoreState.HALT:
            if loop.time() > deadline:
                raise RuntimeError(
                    "bootrom did not reach the halt stub through the "
                    "watchdog boot vector")
            await asyncio.sleep(0.01)
        at = (await core.reg_read([pc]))[pc]
        if at != self.base:
            raise RuntimeError(
                f"core 0 halted at 0x{at:08x}, not at the bootrom halt "
                f"stub (0x{self.base:08x})")

        puppet = _BootromPuppet(
            "bootrom-puppet", core,
            Ram("bootrom-ram", self.base + self.STUB_STACK,
                self.size - self.STUB_STACK),
            debug.mem_ap)
        lookup, = struct.unpack(
            "<H", await debug.mem_read(BOOTROM_TABLE_LOOKUP, 2))
        funcs = {}
        for name, (c1, c2) in BOOTROM_FUNCS.items():
            funcs[name] = await puppet.call(
                lookup, ord(c1) | ord(c2) << 8, RT_FLAG_FUNC_ARM_SEC,
                timeout=2.0)
            if not funcs[name]:
                raise RuntimeError(f"bootrom has no {name} ('{c1}{c2}')")
        self.__puppet = puppet
        self.__funcs = funcs
        self.generation += 1

    def close(self) -> None:
        self.__puppet = None
        self.__funcs = {}

    def allocate(self, size: int):
        return self.__puppet.allocate(size, align=4)

    async def call(self, name: str, *args: int,
                   timeout: float = 2.0) -> int:
        return await self.__puppet.call(self.__funcs[name], *args,
                                        timeout=timeout)


class Rp2350Flash(Flash):
    """External QSPI flash on chip select 0, programmed through the
    bootrom's flash API.

    The region spans the 16 MiB XIP window until :meth:`detect` reads
    the flash's JEDEC ID and shrinks it to the device size. Reads go
    through the uncached XIP alias with the flash in 03h command-XIP
    mode; erase and program switch it back to serial mode
    (``connect_internal_flash`` + ``flash_exit_xip``). Every access
    opens the bootrom helper first, which resets and takes over
    core 0."""

    SECTOR = 0x1000
    BLOCK = 0x10000
    BLOCK_ERASE_CMD = 0xD8
    PROGRAM_ALIGN = 0x100

    READ_CHUNK = 32 * 1024
    # SRAM buffer per flash_range_program call; the bootrom helper's
    # RAM leaves room for 2 KiB next to the puppet's stack.
    PROGRAM_BUFFER = 0x800

    # Per-call timeouts: a 64 KiB block erase is ~2 s worst case.
    ERASE_TIMEOUT_PER_BLOCK = 2.0
    PROGRAM_TIMEOUT = 2.0

    def __init__(self, name, bootrom: Rp2350Bootrom):
        super().__init__(name, XIP_BASE, XIP_WINDOW,
                         write_page_size=self.SECTOR,
                         erase_page_sizes=[self.SECTOR, self.BLOCK])
        self.bootrom = bootrom
        self.detected_size: int | None = None
        self.__generation = None
        self.__serial: bool | None = None  # None: unknown
        self.__buffer = None

    async def __ensure_open(self):
        await self.bootrom.open()
        if self.__generation != self.bootrom.generation:
            self.__generation = self.bootrom.generation
            self.__serial = None
            self.__buffer = None

    async def __serial_mode(self):
        if self.__serial is not True:
            await self.bootrom.call("connect_internal_flash")
            await self.bootrom.call("flash_exit_xip")
            self.__serial = True

    async def __xip_mode(self):
        if self.__serial is not False:
            if self.__serial is None:
                await self.bootrom.call("connect_internal_flash")
            await self.bootrom.call("flash_flush_cache")
            await self.bootrom.call("flash_enter_cmd_xip")
            self.__serial = False

    async def detect(self) -> int | None:
        """Read the JEDEC ID; return the flash size in bytes, or None
        if no flash answers. Resizes the region to the device."""
        await self.__ensure_open()
        await self.__serial_mode()
        manufacturer, memtype, capacity = await self.__read_jedec_id()
        size = 1 << capacity if 16 <= capacity <= 28 else None
        if size is None:
            self.logger.info(
                "no QSPI flash: JEDEC ID %02x %02x %02x",
                manufacturer, memtype, capacity)
        else:
            self.logger.info(
                "QSPI flash: JEDEC ID %02x %02x %02x, %d KiB",
                manufacturer, memtype, capacity, size // 1024)
            self.size = min(size, XIP_WINDOW)
        self.detected_size = size
        return size

    async def __read_jedec_id(self) -> tuple[int, int, int]:
        debug = self.bootrom.debuggable

        async def csr_write(value):
            await debug.mem_write(QMI_DIRECT_CSR, struct.pack("<I", value))

        csr, = struct.unpack("<I", await debug.mem_read(QMI_DIRECT_CSR, 4))
        await csr_write(csr | QMI_CSR_EN)
        await csr_write(csr | QMI_CSR_EN | QMI_CSR_ASSERT_CS0N)
        for byte in (0x9F, 0, 0, 0):
            await debug.mem_write(QMI_DIRECT_TX, struct.pack("<I", byte))
        while struct.unpack(
                "<I", await debug.mem_read(QMI_DIRECT_CSR, 4))[0] \
                & QMI_CSR_BUSY:
            pass
        rx = [struct.unpack(
                  "<I", await debug.mem_read(QMI_DIRECT_RX, 4))[0] & 0xFF
              for _ in range(4)]
        await csr_write(csr | QMI_CSR_EN)
        await csr_write(csr & ~QMI_CSR_EN)
        return rx[1], rx[2], rx[3]

    async def read(self, offset, size):
        await self.__ensure_open()
        if self.detected_size is None and not await self.detect():
            raise RuntimeError("no QSPI flash detected on this RP2350")
        await self.__xip_mode()
        out = bytearray()
        base = XIP_NOCACHE_BASE + offset
        for o in range(0, size, self.READ_CHUNK):
            out += await self.bootrom.debuggable.mem_read(
                base + o, min(self.READ_CHUNK, size - o))
        return bytes(out)

    async def erase(self, offset, size):
        if offset % self.SECTOR or size % self.SECTOR:
            raise ValueError(
                f"RP2350 flash erase must be 4 KiB aligned "
                f"(offset 0x{offset:x}, size 0x{size:x})")
        await self.__ensure_open()
        await self.__serial_mode()
        blocks = -(-size // self.BLOCK)
        with self.progress("erase", size // self.SECTOR, "sectors") as bar:
            await self.bootrom.call(
                "flash_range_erase", offset, size, self.BLOCK,
                self.BLOCK_ERASE_CMD,
                timeout=2.0 + blocks * self.ERASE_TIMEOUT_PER_BLOCK)
            bar.advance(size // self.SECTOR)
        if offset == 0 and size >= self.size:
            self.is_blank = True

    async def _erase_for(self, region_map):
        """Erase the sectors ``region_map`` touches, as contiguous
        ranges: one bootrom call each, which uses 64 KiB block
        erases where it can."""
        sectors = set()
        for addr, data in region_map:
            offset = addr - self.address
            start = offset & ~(self.SECTOR - 1)
            end = (offset + len(data) + self.SECTOR - 1) & ~(self.SECTOR - 1)
            sectors.update(range(start, end, self.SECTOR))
        ranges = []
        for s in sorted(sectors):
            if ranges and ranges[-1][1] == s:
                ranges[-1][1] = s + self.SECTOR
            else:
                ranges.append([s, s + self.SECTOR])
        for start, end in ranges:
            await self.erase(start, end - start)

    async def write(self, offset, data):
        if offset % self.PROGRAM_ALIGN:
            raise ValueError(
                f"RP2350 flash program must be 256-byte aligned "
                f"(offset 0x{offset:x})")
        data = bytes(data)
        pad = -len(data) % self.PROGRAM_ALIGN
        data += bytes([self.erased_value]) * pad
        await self.__ensure_open()
        await self.__serial_mode()
        if self.__buffer is None:
            self.__buffer = self.bootrom.allocate(self.PROGRAM_BUFFER)
        for o in range(0, len(data), self.PROGRAM_BUFFER):
            chunk = data[o:o + self.PROGRAM_BUFFER]
            await self.__buffer.write(chunk)
            await self.bootrom.call(
                "flash_range_program", offset + o, self.__buffer.address,
                len(chunk), timeout=self.PROGRAM_TIMEOUT)


class Rp2350SwdTarget(ArmSocTarget):
    """RP2350 run-control target accessed via its single SW-DP.

    Holds one :class:`CortexMDebuggable` per Cortex-M33 core whose
    AP was discovered with an SCS beneath it, and a
    :class:`Rp2350Loadable` driven through core 0."""


class Rp2350Loadable(Loadable):
    """Programs an RP2350 through core 0's MEM-AP, into SRAM or the
    external QSPI flash depending on where the image loads.

    **SRAM images** (pico-sdk ``no_flash``) are written over the
    MEM-AP and started through the bootrom's watchdog boot vector
    (:func:`_boot_through_vector`): VTOR is pointed at the image —
    pico-sdk's reset handler doesn't set it (only ``_entry_point``
    does), so without this the SDK looks up IRQ handlers in the
    bootrom's table and hard-asserts — and SP and PC come from its
    vector table at the base of SRAM. Starting from a debugger-set PC
    instead doesn't work: after a reset catch the RCP is unseeded, so
    the reset handler faults on its first bootrom call unless entered
    through ``_entry_point``, whose address a raw binary, or ``chip
    reset`` in a later session, doesn't know.

    **Flash images** are written through :class:`Rp2350Flash`, after
    checking a flash is fitted and large enough, and started with a
    plain core 0 reset: the bootrom then boots the flash image as it
    would at power-up. An image may not load into both.

    :meth:`reset` restarts whichever kind is loaded, telling them
    apart by the vector table at the base of SRAM: a no_flash image's
    points into SRAM, while a running flash image leaves its RAM
    vector table there pointing into flash.

    Reading flash (verify, readback) takes over core 0 too, so a
    standalone :meth:`verify` or :meth:`read` that had to restarts the
    image afterwards. :meth:`read` reads SRAM before flash, as opening
    the bootrom helper overwrites the SCRATCH_X/Y banks.

    Core 1 is left alone. If a debugger has it halted, TIMER0/1
    (DBGPAUSE, set at reset) stop counting and an SDK image stalls in
    its first ``sleep_ms``; starting warns about that."""

    VECTOR_TABLE = SRAM_BASE

    # How long the bootrom gets to take the boot vector.
    BOOT_TIMEOUT = 0.5

    def __init__(self, name, debuggable, bootrom: Rp2350Bootrom):
        super().__init__(name)
        self.debuggable = debuggable
        self.bootrom = bootrom
        # "sram" or "flash": where the image being written loads.
        self.__mode: str | None = None
        # Set while write() runs: its verify mustn't restart the image.
        self.__programming = False

    @property
    def flash(self) -> Rp2350Flash | None:
        return next(iter(self.children_of_class(Rp2350Flash)), None)

    async def write(self, source, **kwargs):
        m = (await coerce_memory_map(source)).simplified()
        in_flash = bool(m.within(XIP_BASE, XIP_BASE + XIP_WINDOW))
        in_sram = bool(m.within(SRAM_BASE, SRAM_END))
        if in_flash and in_sram:
            raise ValueError(
                "image loads into both QSPI flash and SRAM; load one "
                "kind of image at a time")
        if in_flash:
            # Detecting the flash opens the bootrom helper; if the image
            # is refused, restart what was there.
            await self.__restarting(self.__require_flash(
                max(addr + len(data) for addr, data in m)),
                on_error_only=True)
        self.__mode = "flash" if in_flash else "sram"
        self.__programming = True
        try:
            return await super().write(m, **kwargs)
        finally:
            self.__programming = False

    async def verify(self, source) -> bool:
        return await self.__restarting(super().verify(source))

    async def read(self, begin=0, end=None):
        async def read_sram_first():
            regions = sorted(self.regions,
                             key=lambda r: isinstance(r, Rp2350Flash))
            stop = end if end is not None else max(r.end for r in regions)
            m = MemoryMap()
            for region in regions:
                lo, hi = max(region.address, begin), min(region.end, stop)
                if lo < hi:
                    m.append(lo, await region.read(lo - region.address,
                                                   hi - lo))
            m.chunks.sort()
            return m
        return await self.__restarting(read_sram_first())

    async def __restarting(self, operation, *, on_error_only=False):
        """Run ``operation``; if it opened the bootrom helper (reading
        flash), restart the image afterwards — or, with
        ``on_error_only``, only if it failed."""
        if self.__programming:
            return await operation
        was_open = self.bootrom.is_open
        failed = True
        try:
            result = await operation
            failed = False
            return result
        finally:
            if (self.bootrom.is_open and not was_open
                    and (failed or not on_error_only)):
                await self.reset()

    async def __require_flash(self, end: int | None = None) -> Rp2350Flash:
        flash = self.flash
        size = await flash.detect() if flash is not None else None
        if not size:
            raise RuntimeError(
                "no QSPI flash detected on this RP2350; only no_flash "
                "(SRAM) images can be loaded")
        if end is not None and end > XIP_BASE + size:
            raise ValueError(
                f"image ends at 0x{end:08x}, past the end of the "
                f"{size // 1024} KiB flash (0x{XIP_BASE + size:08x})")
        return flash

    async def pre_program(self, *, do_erase, assume_clean):
        if self.__mode == "flash":
            # The bootrom helper already has core 0 halted.
            await super().pre_program(do_erase=do_erase,
                                      assume_clean=assume_clean)
        else:
            # Halt core 0 so it doesn't run from SRAM while it's being
            # overwritten. core.halt() rather than Debuggable.attach():
            # attach's enable_debug() used to clear C_HALT, briefly
            # resuming a core that's already halted — fatal once its
            # code has been replaced.
            await self.debuggable.cores[0].halt()

    async def post_program(self, *, success, do_start):
        if do_start and success:
            if self.__mode == "flash":
                await self.boot_flash()
            else:
                await self.start_image()

    async def erase_all(self):
        flash = await self.__require_flash()
        await flash.erase(0, flash.size)

    async def reset(self):
        sp, pc = struct.unpack(
            "<II", await self.debuggable.mem_read(self.VECTOR_TABLE, 8))
        if _sram_vector(sp, pc):
            await self.start_image()
        else:
            await self.boot_flash()

    async def boot_flash(self):
        """Reset core 0 and let the bootrom boot the flash image."""
        self.bootrom.close()
        core = self.debuggable.cores[0]
        await core.halt()
        await self.__warn_if_core1_halted()
        # No stale boot vector: the bootrom must take its normal path.
        await self.debuggable.mem_write(WATCHDOG_SCRATCH4, bytes(4))
        self.logger.info("Booting from flash")
        await core.reset(stop=False)
        # C_HALT survives the core reset; let the bootrom run.
        await core.resume()

    async def start_image(self):
        """Boot the SRAM image whose vector table is at
        :attr:`VECTOR_TABLE` through the bootrom's boot vector."""
        self.bootrom.close()
        debug = self.debuggable
        await debug.cores[0].halt()
        sp, pc = struct.unpack(
            "<II", await debug.mem_read(self.VECTOR_TABLE, 8))
        if not _sram_vector(sp, pc):
            raise RuntimeError(
                f"no no_flash image at 0x{self.VECTOR_TABLE:08x}: vector "
                f"table SP 0x{sp:08x}, reset 0x{pc:08x} don't point "
                f"into SRAM")
        await self.__warn_if_core1_halted()
        self.logger.info("Starting image: SP 0x%08x, PC 0x%08x", sp, pc)
        await _boot_through_vector(debug, sp, pc, vtor=self.VECTOR_TABLE)

        # The bootrom clears SCRATCH4 once it has taken the vector.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.BOOT_TIMEOUT
        while True:
            magic, = struct.unpack(
                "<I", await debug.mem_read(WATCHDOG_SCRATCH4, 4))
            if magic != BOOT_VECTOR_MAGIC:
                break
            if loop.time() > deadline:
                raise RuntimeError(
                    "bootrom did not take the watchdog boot vector "
                    "(SCRATCH4 still set)")
            await asyncio.sleep(0.01)

    async def __warn_if_core1_halted(self):
        target = self.parent
        if target is None:
            return
        for debug in target.children_of_class(CortexMDebuggable):
            if debug is self.debuggable:
                continue
            for core in debug.cores:
                if await core.state() == CoreState.HALT:
                    self.logger.warning(
                        "%s is halted by the debugger: TIMER0/1 are "
                        "paused (DBGPAUSE) until it runs again, so the "
                        "image will stall in its first sleep",
                        debug.name)


@ArmSocTarget.db.register(RP2350_TARGETID)
async def rp2350_swd_probe(dp):
    """Build an Rp2350SwdTarget from the DP whose TARGETID matched.

    Walks the DP's subtree for the core MEM-APs at their fixed
    base addresses. A core whose AP is missing or has no SCS (e.g.
    powered down) is skipped with a warning rather than failing
    the whole Target. Without core 0 there's no Loadable."""
    target = Rp2350SwdTarget("rp2350")
    debuggables = {}
    aps = {ap.base: ap for ap in dp.children_of_class(MemApV2)}
    for base, index in sorted(RP2350_CORE_APS.items()):
        ap = aps.get(base)
        rom_table = None
        if ap is not None:
            rom_table = next(
                (rt for rt in ap.children_of_class(RomTable)
                 if rt.children_of_class(Scs)), None)
        if rom_table is None:
            target.logger.warning(
                "rp2350 core%d: no SCS-bearing ROM table under the AP "
                "at 0x%x — skipped", index, base)
            continue
        target.claim(ap, rom_table)
        debug = CortexMDebuggable.from_romtable(
            rom_table, ap, name=f"debug-core{index}")
        debug.memory_map.append(Ram("rom", ROM_BASE, ROM_SIZE))
        debug.memory_map.append(Ram("sram", SRAM_BASE, SRAM_SIZE))
        target.child_add(debug)
        debuggables[index] = debug

    if not debuggables:
        raise NoMatch(
            "rp2350_swd_probe",
            f"DP {dp.name}: no Cortex-M33 core AP with an SCS")

    core0 = debuggables.get(0)
    if core0 is not None:
        loadable = Rp2350Loadable("main", core0, Rp2350Bootrom(core0))
        loadable.child_add(
            BusRam("sram", SRAM_BASE, SRAM_SIZE, core0.mem_ap))
        loadable.child_add(Rp2350Flash("flash", loadable.bootrom))
        target.child_add(loadable)
    return target
