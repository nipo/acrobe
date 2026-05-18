"""Raspberry Pi RP2040 / RP2350 SWD multidrop target.

The RP2 family exposes one Debug Port per Cortex core plus a
rescue DP on a single shared SWD wire (ADIv5 multidrop, selected
via TARGETSEL). The multidrop bring-up
(:mod:`acrobe.protocol.swd`) already spawns one
:class:`SwDp` per responsive TARGETSEL on the wire; this module
turns that flat set of DPs into a single :class:`Rp2040SwdTarget`
with one :class:`CortexMDebuggable` per core.

Discovery hooks on :class:`Dp` at precedence 500 — lower than
the generic :func:`arm_soc_probe` at 10000, higher than the
chip-specific MCU explorers. The PartId match is a constant
compare against the RP2 family TPARTNO (0x1002 / 0x0004); no
bus reads needed. Once matched, the probe walks the parent
:class:`swd.Interface` for sibling :class:`SwDp` children and
claims them all, so the generic factory does not refire on
them in subsequent discovery passes.

The Target supports an on-demand ``spi`` child: summon
``rp2040/spi/cs0/flash`` and the Target halts core0, builds an
:class:`ArmMPuppet` over its Mem-AP plus a chunk of SRAM,
performs the bootrom-driven SSI bring-up
(:func:`rp2040_exit_xip_via_puppet`) and exposes the QSPI bus as
a generic :class:`spi.Interface` driven by the same
:class:`Rp2040Spi` stub as the PICOBOOT path. Caller's
expectation: the chip will be reset after the SPI session — we
clobber a slice of SRAM and don't restore it.

Known issue (2026-05): RP2040's debug fabric does not pipeline
AP-read responses the way the ADIv5 spec specifies — only the
RDBUFF drain at end of a batch carries valid data, all
intermediate AP-read response slots come back as zeros. Bit-
banged SWD adapters (FTDI, J-Link) therefore can't run a multi-
register :meth:`Cortex.reg_write` batch correctly, which is what
:meth:`ArmMPuppet.prepare` does. SPI-via-SWD is therefore
scaffolded but not functional on this hardware until the wire
layer is taught to drain via RDBUFF after every AP read. The
PICOBOOT path is unaffected (firmware-side pipelining hides the
issue).

Flash programming over SWD (a Loadable that drives
:class:`Rp2040Spi`) is not modelled here yet; the PICOBOOT-rooted
:class:`Rp2040Target` (sibling module ``rp2040.py``) is still the
canonical flashing path.
"""

from __future__ import annotations

from ...component.arm.coresight.rom_table import RomTable
from ...component.arm.coresight.scs import Scs
from ...component.arm.dp import Dp
from ...component.arm.mem_ap import MemAp
from ...component.arm.sw_dp import SwDp
from ...db import NoMatch
from ...part_id import PartId
from ...protocol import swd
from ..puppet import ArmMPuppet, Puppet
from ..region import Ram
from ..target import Target
from .cortex_m import CortexMDebuggable
from .soc import ArmSocTarget


# (TPARTNO, family-name) for chips whose multidrop layout this
# module understands. RP2350 is included pre-emptively — the
# multidrop shape is identical, TPARTNO is the only thing that
# differs between the families.
RP2_FAMILIES = {
    0x1002: "rp2040",
    0x0004: "rp2350",
}


# Top 16 KiB of SRAM — large enough for the SPI stub (124 B), its
# trampoline + data block (~32 B), the per-transaction cmd
# array + tx/rx buffers, and Puppet's stack. Stays well clear of
# typical user-firmware .data / .bss at the bottom of SRAM.
# Caller's contract is that the chip is reset after the puppet
# session — we deliberately clobber this slice.
SWD_PUPPET_RAM_BASE = 0x2003C000
SWD_PUPPET_RAM_SIZE = 0x00004000

# Bootrom function-table magic addresses (RP2040 datasheet
# section 2.8.3). Each location holds a 16-bit pointer.
BOOTROM_ROM_FUNC_TABLE_PTR     = 0x14
BOOTROM_ROM_FUNC_LOOKUP_FN_PTR = 0x18


def _rom_code(c1: str, c2: str) -> int:
    """Encode a two-character bootrom function code."""
    return ord(c1) | (ord(c2) << 8)


async def rp2040_exit_xip_via_puppet(puppet: Puppet) -> None:
    """Equivalent of the PICOBOOT ``exit_xip`` command, run on the
    target through a generic :class:`Puppet`.

    Reads the bootrom's function-lookup pointers (fixed addresses
    in the low 32 bytes of bootrom), invokes
    ``connect_internal_flash`` + ``flash_exit_xip`` via
    :meth:`Puppet.call`. Idempotent — every call leaves the SSI
    block in the manual SR-polled mode the SPI passthrough stub
    expects, regardless of where the chip was beforehand
    (running user code from XIP, halted in the middle of a flash
    transaction, …).
    """
    table_bytes = await puppet.mem_read(BOOTROM_ROM_FUNC_TABLE_PTR, 2)
    lookup_bytes = await puppet.mem_read(BOOTROM_ROM_FUNC_LOOKUP_FN_PTR, 2)
    table_addr = int.from_bytes(table_bytes, "little")
    lookup_addr = int.from_bytes(lookup_bytes, "little")

    # CONNECT_INTERNAL_FLASH ('I','F'): re-routes QSPI pins from
    # XIP to SSI master. Required before EXIT_XIP — without it the
    # SSI block has nothing to drive.
    connect_addr = await puppet.call(
        lookup_addr, table_addr, _rom_code("I", "F"), timeout=2.0)
    if connect_addr == 0:
        raise RuntimeError(
            "RP2040 bootrom: rom_func_lookup('IF') returned 0 — "
            "connect_internal_flash unavailable")

    # FLASH_EXIT_XIP ('E','X'): SSI block out of XIP-read mode,
    # into manual SR-polled transfers.
    exit_addr = await puppet.call(
        lookup_addr, table_addr, _rom_code("E", "X"), timeout=2.0)
    if exit_addr == 0:
        raise RuntimeError(
            "RP2040 bootrom: rom_func_lookup('EX') returned 0 — "
            "flash_exit_xip unavailable")

    await puppet.call(connect_addr, timeout=2.0)
    await puppet.call(exit_addr, timeout=2.0)


class Rp2040SwdTarget(ArmSocTarget):
    """RP2040 / RP2350 run-control target accessed via SWD multidrop.

    Holds one :class:`CortexMDebuggable` per core DP. Exposes
    ``spi`` as an on-demand child: summon it (via
    :meth:`child_summon`) to build an :class:`ArmMPuppet` over
    core0 + the top 16 KiB of SRAM, drive the bootrom EXIT_XIP
    sequence, and expose the QSPI bus as a generic SPI Interface.
    Caller's contract: chip will be reset after the SPI session —
    we don't preserve the SRAM slice we use.

    Loadable for flash programming over SWD is not implemented
    here yet; the PICOBOOT-rooted :class:`Rp2040Target` covers
    flashing today."""

    async def child_spawn(self, name):
        if name == "spi":
            return await self.__spawn_spi()
        return await super().child_spawn(name)

    async def __spawn_spi(self):
        from ...component.raspberry.spi import Rp2040Spi

        debuggables = self.children_of_class(CortexMDebuggable)
        if not debuggables:
            raise NoMatch(
                "rp2040_swd:spi",
                "no Debuggable on this Target — cannot build a puppet")
        core_debuggable = debuggables[0]
        cores = core_debuggable.cores
        if not cores:
            raise NoMatch(
                "rp2040_swd:spi",
                "Debuggable has no Cores — cannot build a puppet")

        # Enable debug + halt core0. The puppet runs target code by
        # clobbering its register state and SRAM; if the core were
        # running user firmware it would race the host on both.
        # ``attach()`` enables DEBUGEN (required before register
        # transfers work) and halts every core on the Debuggable;
        # we use it instead of a bare ``core.halt()`` so the
        # subsequent puppet ``prepare()`` actually finds DCRSR
        # responsive.
        await core_debuggable.attach()
        core = cores[0]

        sram = Ram(
            "sram", SWD_PUPPET_RAM_BASE, SWD_PUPPET_RAM_SIZE)
        puppet = ArmMPuppet(
            "puppet", core, sram, core_debuggable.mem_ap)
        self.child_add(puppet)

        async def ssi_init():
            await rp2040_exit_xip_via_puppet(puppet)

        return Rp2040Spi(puppet, ssi_init=ssi_init, name="spi")


def _rp2_family(partid: PartId) -> str | None:
    """Return the family name (``"rp2040"`` / ``"rp2350"``) if
    ``partid`` matches a known RP2 chip; ``None`` otherwise.
    Compares the JEDEC + TPARTNO fields only — TINSTANCE
    (encoded in PartId.revision) varies per DP."""
    if (partid.jep106_bank == 9
            and partid.jep106_id == 0x13
            and partid.part_no in RP2_FAMILIES):
        return RP2_FAMILIES[partid.part_no]
    return None


@Target.register(Dp, precedence=500)
async def rp2040_swd_probe(dp):
    """Build one Rp2040SwdTarget out of every RP2 DP on this wire.

    Triggered once per Dp (whichever fires first wins). After
    claiming all sibling DPs, subsequent firings on the same wire
    raise :class:`NoMatch` because the chip's PartId is no longer
    on any unclaimed Dp.
    """
    chip = dp.chip_id()
    if chip is None:
        raise NoMatch("rp2040_swd_probe", f"DP {dp.name} has no chip_id")
    family = _rp2_family(chip.partid)
    if family is None:
        raise NoMatch(
            "rp2040_swd_probe",
            f"DP {dp.name} chip_id {chip.partid.pretty()} is not RP2")

    # Walk the parent swd.Interface for sibling SwDps. Each
    # MultidropSwDp carries its own TARGETSEL; we identify cores
    # by TARGETSEL's TINSTANCE (encoded as PartId.revision).
    iface = dp.parent_of_class(swd.Interface)
    if iface is None:
        # JTAG-DP or an unusual wiring — bail out, generic factory
        # picks it up as a single-DP Cortex-M target.
        raise NoMatch(
            "rp2040_swd_probe",
            f"DP {dp.name} is not on an swd.Interface")

    sibling_dps = iface.children_of_class(SwDp)
    cores: dict[int, SwDp] = {}    # TINSTANCE -> core DP
    rescue: SwDp | None = None
    for sib in sibling_dps:
        if sib.targetsel is None:
            continue
        sib_partid = PartId.from_idcode(sib.targetsel)
        if _rp2_family(sib_partid) != family:
            continue
        tinstance = sib_partid.revision
        if tinstance == 0xf:
            rescue = sib
        else:
            cores[tinstance] = sib

    if not cores:
        # Wire is in rescue-only state (chip stuck or held in
        # reset). Decline so the user can still target the rescue
        # DP directly to recover the chip.
        raise NoMatch(
            "rp2040_swd_probe",
            f"{family}: no core DP responded; only rescue present")

    target = Rp2040SwdTarget(family)
    target.claim(*cores.values())
    if rescue is not None:
        target.claim(rescue)

    # One Debuggable per core. Each core's DP carries its own
    # AHB-AP with its own CoreSight ROM Table + SCS subtree (the
    # multidrop layout is symmetric across cores).
    for tinstance in sorted(cores):
        core_dp = cores[tinstance]
        debuggable = _build_core_debuggable(
            core_dp, name=f"debug-core{tinstance}")
        if debuggable is None:
            target.logger.warning(
                "%s core%d: no SCS-bearing ROM table under AHB-AP — "
                "skipped",
                family, tinstance)
            continue
        target.child_add(debuggable)

    return target


def _build_core_debuggable(core_dp, *, name):
    """Construct a CortexMDebuggable from the first AHB-AP under
    ``core_dp`` that carries a ROM table with an SCS. Returns
    ``None`` if no suitable subtree exists (rescue-shaped DPs)."""
    for ap in core_dp.children_of_class(MemAp):
        for rt in ap.children_of_class(RomTable):
            if not rt.children_of_class(Scs):
                continue
            return CortexMDebuggable.from_romtable(rt, ap, name=name)
    return None
