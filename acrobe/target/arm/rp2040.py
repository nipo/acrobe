"""Raspberry Pi RP2040, accessed via the PICOBOOT bootloader.

When the chip is in BOOTSEL mode it enumerates as USB 0x2e8a:0x0003
and the on-chip ROM exposes a vendor-class command interface that
can read/write any address in the chip's view (including XIP-mapped
flash) and erase/program flash through bootrom helpers. We use
those commands directly for `chip program` / `chip verify` /
`chip read`; no on-target stub is required for basic flash
programming.

The Target owns the chip's single `PicobootPuppet` and is the only
place a puppet is constructed — the adapter / component layer is
intentionally puppet-free so all stub-running code paths (SPI
passthrough, SFDP probe, future helpers) share one SRAM allocator.
The reserved 4 KiB at the top of SRAM stays clear of the bootrom's
USB DPRAM mirror workspace.

At probe time we spawn `spi/cs0/flash` under the Target to drive
the SPI passthrough stub, ask the SpiFlash component to read
JEDEC ID + SFDP for size and sector geometry, capture the
results, and tear the subtree back down. The user can later
respawn `spi/cs0/flash` on demand (path resolution will call
back into `child_spawn`).

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
from ...db import NoMatch
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

# Fallback geometry when SFDP detection fails — covers the most
# common Pico boards (W25Q16, 2 MiB) and the conservative
# subset of erase / page sizes every standard SPI NOR supports.
DEFAULT_FLASH_SIZE        = 2 * 1024 * 1024
DEFAULT_FLASH_PAGE_SIZE   = 256
DEFAULT_FLASH_SECTOR_SIZE = 4096


class Rp2040Target(CortexMTarget):
    """RP2040 — dual Cortex-M0+, QSPI flash with XIP. Accessed here
    through PICOBOOT; the SWD-driven path will land as a sibling
    Target file once it's needed.

    Owns the chip's `PicobootPuppet` and serves as the parent for
    on-demand `spi` subtrees (`spi/cs0/flash` etc.). The Target
    is the single owner of the SRAM allocator: every stub-running
    path goes through this Target's puppet."""

    def __init__(self, name: str, picoboot: Picoboot,
                 puppet: PicobootPuppet):
        super().__init__(name)
        self.picoboot = picoboot
        self.puppet = puppet

    async def child_spawn(self, name):
        if name == "spi":
            from ...component.raspberry.spi import Rp2040Spi
            # PICOBOOT transport exposes EXIT_XIP as a vendor USB
            # command — no on-target stub work needed here.
            return Rp2040Spi(
                self.puppet,
                ssi_init=self.picoboot.transport.exit_xip,
                name="spi")
        return await super().child_spawn(name)


class PicobootXipFlash(Flash):
    """RP2040 XIP-mapped QSPI flash, programmed via PICOBOOT.

    The bootrom's READ / WRITE / FLASH_ERASE commands handle all
    SSI register choreography for us — no on-target stub needed.
    Reads of XIP-range addresses transparently re-enter XIP first;
    writes auto-route into the page-program path; erase aligns to
    the chip's smallest sector.
    """

    def __init__(self, name, address, size, picoboot: Picoboot, *,
                 write_page_size: int = DEFAULT_FLASH_PAGE_SIZE,
                 erase_page_sizes=None):
        super().__init__(
            name, address, size,
            write_page_size=write_page_size,
            erase_page_sizes=erase_page_sizes
                or [DEFAULT_FLASH_SECTOR_SIZE])
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
    operations, dispatches whole-chip erase to the SPI chip-erase
    fast path, and (on `do_start`) reboots the chip out of BOOTSEL
    into the freshly-programmed firmware."""

    def __init__(self, name: str, picoboot: Picoboot):
        super().__init__(name)
        self.picoboot = picoboot

    async def pre_program(self, *, do_erase, assume_clean):
        # Exclusive-access mode 1 = exclusive (kicks out the USB MSC
        # interface so its auto-mount can't probe flash mid-program).
        await self.picoboot.transport.exclusive_access(1)
        # Delegate to the base class so do_erase / assume_clean
        # actually take effect — our override only adds the
        # exclusive-access bracket.
        await super().pre_program(
            do_erase=do_erase, assume_clean=assume_clean)

    async def erase_all(self):
        """Whole-chip erase via the SPI passthrough's chip-erase
        command (single 0xC7) — for an 8 MiB GD25Q64 that's one
        SPI transaction vs ~2k per-sector FLASH_ERASE commands
        through the bootrom (each one taking 30–50 ms on the wire).

        Falls back to the default per-region erase loop if the SPI
        path is unavailable for any reason.
        """
        target = self.parent
        spi_ok = False
        try:
            flash = await target.child_summon("spi", "cs0", "flash")
            self.logger.note(
                "Mass-erase via SPI chip-erase (JEDEC 0x%06x)",
                flash.jedec_id)
            await flash.erase_chip()
            spi_ok = True
        except Exception as e:
            self.logger.warning(
                "SPI chip-erase failed (%s); falling back to "
                "per-sector erase via bootrom", e)
        finally:
            # Tear the SPI subtree down whether erase succeeded or
            # not — keeps `info target` clean and returns the stub
            # zone to the puppet's allocator.
            spi = target.child_lookup("spi")
            if spi is not None:
                try:
                    await target.child_remove(spi)
                except Exception:
                    pass
        if spi_ok:
            for f in self.children_of_class(Flash):
                f.is_blank = True
        else:
            await super().erase_all()

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
            await self.picoboot.transport.reboot(
                pc=0, sp=0, delay_ms=100)


async def _detect_flash_geometry(target: Rp2040Target):
    """Spawn `spi/cs0/flash` under `target`, harvest the SpiFlash
    component's identification + SFDP results, then tear the
    subtree back down so the user-visible Target stays clean.
    A user that wants raw SPI access later can re-summon
    `spi/cs0/flash` on demand — the path resolves through
    `Rp2040Target.child_spawn("spi")`.

    Returns ``(total_size, write_page_size, erase_page_sizes)`` on
    success, or ``None`` if detection failed. Errors are logged
    at TRACE; the probe falls back to defaults rather than
    declining the chip.
    """
    info = None
    try:
        flash = await target.child_summon("spi", "cs0", "flash")
        info = (flash.total_size, flash.page_size,
                sorted(s for s, _cmd in flash.sector_info))
        target.logger.note(
            "SFDP detect: %d KiB, page=%d, sectors=%s",
            info[0] // 1024, info[1], info[2])
    except Exception as e:
        target.logger.trace("SFDP detect failed: %s", e)
    finally:
        # Always release the SPI subtree — even on partial spawn,
        # so the user's `info target` doesn't show scaffolding.
        spi = target.child_lookup("spi")
        if spi is not None:
            try:
                await target.child_remove(spi)
            except Exception as e:
                target.logger.trace(
                    "SPI teardown after SFDP failed: %s", e)
    return info


@Target.register(Picoboot, precedence=500)
async def rp2040_picoboot_probe(picoboot: Picoboot):
    """Build the RP2040 Target rooted at a `Picoboot` Node.

    No chip-side identification step — the existence of a Picoboot
    means the chip is in BOOTSEL, and the only family we currently
    wire here is RP2040. Future RP2350 support belongs in a sibling
    probe (different SRAM size, dual M33, different bootrom).
    """
    adapter = picoboot.parent
    suffix = (adapter.name.removeprefix("rp2040-bootsel-")
              if adapter is not None else "")
    name = f"rp2040-{suffix}" if suffix else "rp2040"

    sram = Ram("sram", SRAM_BASE, PUPPET_RAM_SIZE)
    puppet = PicobootPuppet("puppet", sram, picoboot)
    target = Rp2040Target(name, picoboot, puppet)
    target.claim(picoboot)
    target.child_add(puppet)

    geometry = await _detect_flash_geometry(target)
    if geometry is not None:
        flash_size, page_size, sector_sizes = geometry
    else:
        target.logger.warning(
            "Flash geometry detection failed; assuming "
            "%d KiB / page=%d / sector=%d",
            DEFAULT_FLASH_SIZE // 1024,
            DEFAULT_FLASH_PAGE_SIZE,
            DEFAULT_FLASH_SECTOR_SIZE)
        flash_size = DEFAULT_FLASH_SIZE
        page_size = DEFAULT_FLASH_PAGE_SIZE
        sector_sizes = [DEFAULT_FLASH_SECTOR_SIZE]

    loadable = PicobootLoadable("main", picoboot)
    loadable.child_add(PicobootXipFlash(
        "flash", XIP_BASE, flash_size, picoboot,
        write_page_size=page_size,
        erase_page_sizes=sector_sizes))
    target.child_add(loadable)

    return target
