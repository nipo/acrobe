"""Raspberry Pi RP2040, accessed via the PICOBOOT bootloader.

When the chip is in BOOTSEL mode it enumerates as USB 0x2e8a:0x0003
and the on-chip ROM exposes a vendor-class command interface that
can read/write any address in the chip's view (including XIP-mapped
flash) and erase/program flash through bootrom helpers. We use
those commands directly for `chip program` / `chip verify` /
`chip read`; no on-target stub is required for basic flash
programming.

The Target also hosts a `PicobootPuppet` so future code can run
arbitrary on-target stubs through the same transport — same surface
as `ArmMPuppet` on the SWD-driven path. Reserving puppet RAM at the
top of SRAM steers clear of the bootrom's workspace at the very
end (USB DPRAM mirror, etc.).

Discovery runs against a `Picoboot` Node — produced by
`PicobootAdapter.child_spawn("picoboot")`. Having a `Picoboot`
parent implies the chip is in BOOTSEL: that's all the
identification we need. RP2350 (PID 0x000f) would land here too
when its adapter wiring is added; the chip-specific differences
(flash size, M33 vs M0+) would require a separate target file
once the bring-up is needed.
"""

from __future__ import annotations

from ...component.raspberry.picoboot import Picoboot, PicobootPuppet
from ..loadable import Loadable
from ..region import Flash, Ram
from ..target import Target
from .cortex_m import CortexMTarget


# RP2040 memory map.
XIP_BASE         = 0x10000000
SRAM_BASE        = 0x20000000
SRAM_SIZE        = 0x42000          # 264 KiB
# Reserve the last 4 KiB of SRAM for the bootrom's own use.
PUPPET_RAM_SIZE  = SRAM_SIZE - 0x1000

# QSPI flash page / sector sizes assumed for the bootrom commands.
FLASH_PAGE_SIZE   = 256
FLASH_SECTOR_SIZE = 4096

# Default flash size — matches the most common Pico boards
# (W25Q16, 2 MiB). The PICOBOOT command set has no way to query
# the flash size at runtime; reading SFDP via an SSI driver stub
# is a follow-up. Setting this too small means we'll refuse to
# program a binary that fits the actual chip; too large means
# erase / verify will run past the end and the bootrom will reject
# the address. 2 MiB is the safe-conservative default for Pico-
# family boards.
DEFAULT_FLASH_SIZE = 2 * 1024 * 1024


class Rp2040Target(CortexMTarget):
    """RP2040 — dual Cortex-M0+, QSPI flash with XIP. Accessed here
    through PICOBOOT; the SWD-driven path will land as a sibling
    Target file once it's needed."""


class PicobootXipFlash(Flash):
    """RP2040 XIP-mapped QSPI flash, programmed via PICOBOOT.

    The bootrom's READ / WRITE / FLASH_ERASE commands handle all
    SSI register choreography for us — no on-target stub needed.
    Reads of XIP-range addresses transparently re-enter XIP first;
    writes auto-route into the page-program path; erase aligns to
    4 KiB sectors.
    """

    def __init__(self, name, address, size, picoboot: Picoboot):
        super().__init__(
            name, address, size,
            write_page_size=FLASH_PAGE_SIZE,
            erase_page_sizes=[FLASH_SECTOR_SIZE])
        self.picoboot = picoboot

    async def read(self, offset, size):
        return await self.picoboot.transport.read(
            self.address + offset, size)

    async def erase(self, offset, size):
        await self.picoboot.transport.flash_erase(
            self.address + offset, size)

    async def write(self, offset, data):
        await self.picoboot.transport.write(
            self.address + offset, bytes(data))


class PicobootLoadable(Loadable):
    """Loadable that takes/releases exclusive access around flash
    operations and, when `do_start` is set, reboots the chip out
    of BOOTSEL into the freshly-programmed firmware."""

    def __init__(self, name: str, picoboot: Picoboot):
        super().__init__(name)
        self.picoboot = picoboot

    async def pre_program(self, *, do_erase, assume_clean):
        # Exclusive-access mode 1 = exclusive (kicks out the USB MSC
        # interface so its auto-mount can't probe flash mid-program).
        await self.picoboot.transport.exclusive_access(1)

    async def post_program(self, *, success, do_start):
        # Release exclusive access before optionally rebooting —
        # the REBOOT command needs the device to be willing to
        # finalise it, and exclusive-access state otherwise stays
        # until close.
        try:
            await self.picoboot.transport.exclusive_access(0)
        except Exception:
            pass
        if do_start and success:
            # Reboot into the application: pc=0 / sp=0 with a 100 ms
            # delay lets the bootrom relaunch the flash boot path.
            await self.picoboot.transport.reboot(pc=0, sp=0, delay_ms=100)


@Target.register(Picoboot, precedence=500)
async def rp2040_picoboot_probe(picoboot: Picoboot):
    """Build the RP2040 Target rooted at a `Picoboot` Node.

    No chip-side identification step — the existence of a Picoboot
    means the chip is in BOOTSEL, and the only family we currently
    wire here is RP2040. Future RP2350 support belongs in a sibling
    probe (different SRAM size, dual M33, different bootrom).
    """
    # Picoboot sits under PicobootAdapter, whose name already carries
    # the chip's USB serial (e.g. rp2040-bootsel-e0c9125b0d9b). Reuse
    # that so multiple boards parent at distinct Target paths.
    adapter = picoboot._parent
    suffix = (adapter.name.removeprefix("rp2040-bootsel-")
              if adapter is not None else "")
    target = Rp2040Target(f"rp2040-{suffix}" if suffix else "rp2040")
    target.claim(picoboot)

    loadable = PicobootLoadable("main", picoboot)
    loadable.child_add(PicobootXipFlash(
        "flash", XIP_BASE, DEFAULT_FLASH_SIZE, picoboot))
    target.child_add(loadable)

    sram = Ram("sram", SRAM_BASE, PUPPET_RAM_SIZE)
    puppet = PicobootPuppet("puppet", sram, picoboot)
    target.child_add(puppet)

    return target
