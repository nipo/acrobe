"""System Control Space (SCS). Cortex-M debug + control register
bank at the well-known address 0xE000_E000.

Identified by ARM PartId — different cores have different part_no:
0x000 (Cortex-M3), 0x008 (Cortex-M0), 0x009 (Cortex-M0+),
0x00C (Cortex-M4), 0x00D (Cortex-M7), 0x00E (Cortex-M33),
etc. ARMv8-M cores additionally advertise via DEVARCH
ARCHID = 0x2A04."""

from __future__ import annotations

from dataclasses import dataclass

from ..cpuid import Cpuid
from ..dp import DpAccessFailure
from .model import DevArch, MemoryMappedComponent, PartId


@dataclass(frozen=True)
class CpuFeatures:
    """Snapshot of the SCS feature-ID registers an :class:`Scs`
    instance reports. Optional registers (``mvfr0..2`` for the FPU,
    ``clidr``/``ctr``/``ccsidr`` for the cache hierarchy) come back
    as ``None`` on cores that don't implement them. Each value is
    the raw 32-bit register read; decoding is the caller's
    responsibility — :meth:`Scs.dump_cpu` provides one canonical
    format."""

    pfr0: int | None = None
    pfr1: int | None = None
    dfr0: int | None = None
    afr0: int | None = None
    mmfr0: int | None = None
    mmfr1: int | None = None
    mmfr2: int | None = None
    mmfr3: int | None = None
    isar0: int | None = None
    isar1: int | None = None
    isar2: int | None = None
    isar3: int | None = None
    isar4: int | None = None
    isar5: int | None = None
    mvfr0: int | None = None
    mvfr1: int | None = None
    mvfr2: int | None = None
    clidr: int | None = None
    ctr: int | None = None
    ccsidr: int | None = None


class Scs(MemoryMappedComponent):
    FRIENDLY_NAME = "System Control Space"

    # System Control Space register offsets (from SCS_BASE).
    CPUID_OFFSET   = 0xD00
    AIRCR_OFFSET   = 0xD0C
    CCR_OFFSET     = 0xD14

    # Feature ID block (0xD40..0xD74 — ARMv7-M and ARMv8-M).
    PFR0_OFFSET    = 0xD40
    PFR1_OFFSET    = 0xD44
    DFR0_OFFSET    = 0xD48
    AFR0_OFFSET    = 0xD4C
    MMFR0_OFFSET   = 0xD50
    MMFR1_OFFSET   = 0xD54
    MMFR2_OFFSET   = 0xD58
    MMFR3_OFFSET   = 0xD5C
    ISAR0_OFFSET   = 0xD60
    ISAR1_OFFSET   = 0xD64
    ISAR2_OFFSET   = 0xD68
    ISAR3_OFFSET   = 0xD6C
    ISAR4_OFFSET   = 0xD70
    ISAR5_OFFSET   = 0xD74

    # Cache type / level info (Cortex-M7+, ARMv8-M).
    CLIDR_OFFSET   = 0xD78
    CTR_OFFSET     = 0xD7C
    CCSIDR_OFFSET  = 0xD80

    # Debug Exception and Monitor Control Register (within SCS, at
    # offset 0xDFC from SCS_BASE = 0xE000_E000).
    DEMCR_OFFSET   = 0xDFC
    DEMCR_TRCENA   = 1 << 24

    # FPU feature registers (ARMv7-M / ARMv8-M only, 0xF40..0xF48).
    MVFR0_OFFSET   = 0xF40
    MVFR1_OFFSET   = 0xF44
    MVFR2_OFFSET   = 0xF48

    # CCR bit positions we surface in the human dump. Names align
    # with ARMv7-M reference manual; ARMv8-M adds bits we ignore here.
    CCR_DC         = 1 << 16   # data cache enabled (ARMv7-M+)
    CCR_IC         = 1 << 17   # instruction cache enabled
    CCR_BP         = 1 << 18   # branch prediction enabled

    def __init__(self, bus, base: int, ids, name: str | None = None):
        super().__init__(bus, base, ids, name=name)
        self.cpuid: Cpuid | None = None
        self.features: CpuFeatures | None = None

    async def start(self) -> None:
        """Read CPUID, log the decoded core identification, and set
        DEMCR.TRCENA so ITM/DWT/TPIU come out of reset and their
        CoreSight ID registers become readable.

        Started automatically when this Scs is parented to its
        ROM Table (Node tree's child_add → start_tree on a started
        parent). Sibling ROM-table entries that depend on TRCENA
        (the trace components, typically listed after the SCS) get
        enumerated correctly as long as the SCS appears in the
        ROM Table before its dependent modules and the parent
        awaits start_tree before walking the next entry."""
        try:
            self.cpuid = await self.read_cpuid()
            self.logger.info("CPUID 0x%08x — %s",
                             self.cpuid.raw, self.cpuid.pretty())
        except DpAccessFailure as exc:
            self.logger.warning("CPUID read failed: %s", exc)

        await self._enable_trcena()

    # -- CPUID + features ------------------------------------------

    async def read_cpuid(self) -> Cpuid:
        """Read and decode the SCS CPUID register."""
        raw = await self._bus.read32(self.base + self.CPUID_OFFSET)
        return Cpuid.from_raw(raw)

    async def read_features(self) -> CpuFeatures:
        """Batch-read the optional feature-ID block (ID_PFR0/1, DFR0,
        AFR0, MMFR0..3, ISAR0..5) and the FPU MVFR0..2 + cache
        hierarchy registers, where present.

        Reads that fault are folded back into the dataclass as
        ``None`` (Cortex-M0/M0+ don't implement most of these, and
        we want one read_features call to "just work" across the
        family without each caller reasoning about which registers
        exist on which core)."""
        async def safe(offset: int) -> int | None:
            try:
                return await self._bus.read32(self.base + offset)
            except DpAccessFailure:
                return None

        # asyncio.gather gives us one wire round-trip for the whole
        # feature block on backends that batch DAP transfers.
        import asyncio
        keys = (
            "pfr0", "pfr1", "dfr0", "afr0",
            "mmfr0", "mmfr1", "mmfr2", "mmfr3",
            "isar0", "isar1", "isar2", "isar3", "isar4", "isar5",
            "mvfr0", "mvfr1", "mvfr2",
            "clidr", "ctr", "ccsidr",
        )
        offsets = (
            self.PFR0_OFFSET, self.PFR1_OFFSET, self.DFR0_OFFSET, self.AFR0_OFFSET,
            self.MMFR0_OFFSET, self.MMFR1_OFFSET, self.MMFR2_OFFSET, self.MMFR3_OFFSET,
            self.ISAR0_OFFSET, self.ISAR1_OFFSET, self.ISAR2_OFFSET,
            self.ISAR3_OFFSET, self.ISAR4_OFFSET, self.ISAR5_OFFSET,
            self.MVFR0_OFFSET, self.MVFR1_OFFSET, self.MVFR2_OFFSET,
            self.CLIDR_OFFSET, self.CTR_OFFSET, self.CCSIDR_OFFSET,
        )
        values = await asyncio.gather(*(safe(o) for o in offsets))
        return CpuFeatures(**dict(zip(keys, values)))

    async def dump_cpu(self) -> list[str]:
        """Return a multi-line, human-readable identification dump:
        CPUID + the most useful feature flags. Each line is one
        `key: value` entry; callers print or log them as they like.

        Slice 8's grand finale: the headline output of
        ``acrobe info cpu``."""
        lines: list[str] = []
        if self.cpuid is None:
            try:
                self.cpuid = await self.read_cpuid()
            except DpAccessFailure as exc:
                lines.append(f"CPUID read failed: {exc}")
                return lines
        lines.append(f"CPUID         0x{self.cpuid.raw:08x}  "
                     f"{self.cpuid.pretty()}")
        lines.append(f"  implementer 0x{self.cpuid.implementer:02x}  "
                     f"{self.cpuid.implementer_name}")
        lines.append(f"  architecture 0x{self.cpuid.architecture:x}  "
                     f"{self._architecture_name(self.cpuid.architecture)}")
        lines.append(f"  part_no     0x{self.cpuid.part_no:03x}  "
                     f"{self.cpuid.part_name}")
        lines.append(f"  revision    {self.cpuid.revision_name}")

        feats = await self.read_features()
        self.features = feats

        # FPU support — derived from MVFR* (ARMv7-M / ARMv8-M only).
        if feats.mvfr0 is not None:
            mvfr0, mvfr1 = feats.mvfr0, feats.mvfr1 or 0
            sp = ((mvfr0 >> 4) & 0xF) != 0
            dp = ((mvfr0 >> 8) & 0xF) != 0
            sqrt = ((mvfr0 >> 20) & 0xF) != 0
            div  = ((mvfr0 >> 16) & 0xF) != 0
            fmac = ((mvfr1 >> 28) & 0xF) != 0
            half = ((mvfr1 >> 20) & 0xF) != 0
            mve  = ((mvfr1 >> 8)  & 0xF)
            details = []
            if sp: details.append("SP")
            if dp: details.append("DP")
            if half: details.append("HP")
            if sqrt: details.append("sqrt")
            if div: details.append("div")
            if fmac: details.append("fmac")
            mve_name = {0: None, 1: "MVE-int", 2: "MVE-fp"}.get(mve)
            if mve_name: details.append(mve_name)
            lines.append(f"  fpu         {('yes — ' + ', '.join(details)) if details else 'no'}")

        # ISA features summarised from ISAR0..5.
        if feats.isar0 is not None:
            isar0 = feats.isar0
            isar2 = feats.isar2 or 0
            divide = ((isar0 >> 24) & 0xF) != 0
            mac = ((isar2 >> 12) & 0xF) >= 1
            lines.append(f"  div         {'yes' if divide else 'no'}")
            lines.append(f"  mac         {'yes' if mac else 'no'}")

        # Programmer's model from PFR1 (Security extension).
        if feats.pfr1 is not None:
            sec = (feats.pfr1 >> 4) & 0xF
            sec_name = {0: "no", 1: "yes", 3: "yes (with state)"}.get(
                sec, f"0x{sec:x}")
            lines.append(f"  security    {sec_name}")

        # Cache hierarchy — only on CM7 / ARMv8-M cores that
        # populate CLIDR.
        if feats.clidr:
            lines.append(f"  clidr       0x{feats.clidr:08x}")
            if feats.ctr is not None:
                lines.append(f"  ctr         0x{feats.ctr:08x}")

        # Raw register dump as a footer — useful when chasing chip
        # quirks against the reference manual.
        rawreg = []
        for k, v in (
                ("PFR0", feats.pfr0), ("PFR1", feats.pfr1),
                ("DFR0", feats.dfr0), ("AFR0", feats.afr0),
                ("MMFR0", feats.mmfr0), ("MMFR1", feats.mmfr1),
                ("MMFR2", feats.mmfr2), ("MMFR3", feats.mmfr3),
                ("ISAR0", feats.isar0), ("ISAR1", feats.isar1),
                ("ISAR2", feats.isar2), ("ISAR3", feats.isar3),
                ("ISAR4", feats.isar4), ("ISAR5", feats.isar5),
                ("MVFR0", feats.mvfr0), ("MVFR1", feats.mvfr1),
                ("MVFR2", feats.mvfr2),
        ):
            if v is None:
                continue
            rawreg.append(f"{k}=0x{v:08x}")
        if rawreg:
            lines.append("  raw         " + " ".join(rawreg))

        return lines

    @staticmethod
    def _architecture_name(arch: int) -> str:
        return {
            0xC: "ARMv6-M",
            0xF: "ARMv7-M / ARMv8-M",
        }.get(arch, f"unknown(0x{arch:x})")

    # -- TRCENA ----------------------------------------------------

    async def _enable_trcena(self) -> None:
        addr = self.base + self.DEMCR_OFFSET
        try:
            demcr = await self._bus.read32(addr)
        except DpAccessFailure as exc:
            self.logger.warning(
                "DEMCR read failed (0x%x): %s — trace components "
                "may not enumerate", addr, exc)
            return
        if demcr & self.DEMCR_TRCENA:
            return
        self.logger.info(
            "Enabling DEMCR.TRCENA (DEMCR was 0x%08x) so trace "
            "components are accessible", demcr)
        try:
            await self._bus.write32(addr, demcr | self.DEMCR_TRCENA)
        except DpAccessFailure as exc:
            self.logger.warning(
                "DEMCR write failed: %s — trace components may "
                "not enumerate", exc)


for _part in (0x000,  # Cortex-M3
              0x008,  # Cortex-M0
              0x009,  # Cortex-M0+
              0x00C,  # Cortex-M4
              0x00D,  # Cortex-M7
              0x00E,  # Cortex-M33
              0x471,  # Cortex-M1 (legacy)
              0x4C8): # Cortex-M55
    MemoryMappedComponent.db.register(
        PartId(jep106_bank=4, jep106_id=0x3B, part_no=_part)
    )(Scs)


# ARMv8-M debug architecture.
MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x2A04, revision=0, present=True)
)(Scs)
