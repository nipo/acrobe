"""System Control Space (SCS). Cortex-M debug + control register
bank at the well-known address 0xE000_E000.

Identified by ARM PartId — different cores have different part_no:
0x000 (Cortex-M3), 0x008 (Cortex-M0), 0x009 (Cortex-M0+),
0x00C (Cortex-M4), 0x00D (Cortex-M7), 0x00E (Cortex-M33),
etc. ARMv8-M cores additionally advertise via DEVARCH
ARCHID = 0x2A04.

Beyond CPUID + feature decode, the SCS owns the Cortex-M debug
verbs: `cpu_halt`, `cpu_step`, `cpu_resume`, `cpu_reset`,
`cpu_regs_get/set`, plus the DEMCR vector-catch toggles. Status
is exposed as raw DHCSR / DFSR reads — translation into the
target tree's `CoreState` / `HaltCause` lives at the `CortexMCore`
layer to keep this component CPU-state-vocabulary-agnostic."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..cpuid import Cpuid, FEATURE_REGISTERS
from ..dp import DpAccessFailure
from .model import DevArch, MemoryMappedComponent, PartId


@dataclass(frozen=True, slots=True)
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

_parts = (0x000,  # Cortex-M3
          0x008,  # Cortex-M0
          0x009,  # Cortex-M0+
          0x00C,  # Cortex-M4
          0x00D,  # Cortex-M7
          0x00E,  # Cortex-M33
          0x471,  # Cortex-M1 (legacy)
          0x4C8)  # Cortex-M55
@MemoryMappedComponent.db.register(
    *[PartId(jep106_bank=4, jep106_id=0x3B, part_no=p) for p in _parts])

# ARMv8-M debug architecture.
@MemoryMappedComponent.devarch_db.register(
    DevArch(architect=0x23B, archid=0x2A04, revision=0, present=True)
)

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

    # Debug Halting Control and Status Register.
    DHCSR_OFFSET      = 0xDF0
    DHCSR_KEY         = 0xA05F0000
    DHCSR_S_RESET_ST  = 1 << 25
    DHCSR_S_RETIRE_ST = 1 << 24
    DHCSR_S_LOCKUP    = 1 << 19
    DHCSR_S_SLEEP     = 1 << 18
    DHCSR_S_HALT      = 1 << 17
    DHCSR_S_REGRDY    = 1 << 16
    DHCSR_C_MASKINTS  = 1 << 3
    DHCSR_C_STEP      = 1 << 2
    DHCSR_C_HALT      = 1 << 1
    DHCSR_C_DEBUGEN   = 1 << 0

    # Debug Core Register Selector / Data Register (CPU reg I/O).
    DCRSR_OFFSET      = 0xDF4
    DCRSR_WRITE       = 1 << 16
    DCRDR_OFFSET      = 0xDF8

    # Debug Exception and Monitor Control Register (within SCS, at
    # offset 0xDFC from SCS_BASE = 0xE000_E000).
    DEMCR_OFFSET   = 0xDFC
    DEMCR_TRCENA   = 1 << 24
    DEMCR_VC_HARDERR   = 1 << 10
    DEMCR_VC_INTERR    = 1 << 9
    DEMCR_VC_BUSERR    = 1 << 8
    DEMCR_VC_STATERR   = 1 << 7
    DEMCR_VC_CHKERR    = 1 << 6
    DEMCR_VC_NOCPERR   = 1 << 5
    DEMCR_VC_MMERR     = 1 << 4
    DEMCR_VC_CORERESET = 1 << 0

    # AIRCR — used to trigger SYSRESETREQ.
    AIRCR_KEY         = 0x05FA0000
    AIRCR_SYSRESETREQ = 1 << 2

    # Debug Fault Status Register — bit-encoded "why did we halt".
    DFSR_OFFSET       = 0xD30
    DFSR_CLEAR        = 0x1F
    DFSR_HALTED       = 1 << 0
    DFSR_BKPT         = 1 << 1
    DFSR_DWTTRAP      = 1 << 2
    DFSR_VCATCH       = 1 << 3
    DFSR_EXTERNAL     = 1 << 4

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
        raw = await self.reg_read(self.CPUID_OFFSET)
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
                return await self.reg_read(offset)
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

    async def dump_cpu(self, *, verbose: bool = False) -> list[str]:
        """Return a multi-line, human-readable identification dump:
        CPUID, then a one-line summary of FPU / div / MAC / security
        feature flags, and (when ``verbose`` is set) a fully
        decoded breakdown of every implemented feature register
        via :class:`acrobe.bitfield.Bitfield.dump_pretty`.

        Slice 8's headline output: the answer to "what core is on
        the other end of this wire?" all the way down to which ISA
        extensions, cache topology, and FP variants it implements."""
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
                     f"{self.cpuid.architecture_name}")
        lines.append(f"  part_no     0x{self.cpuid.part_no:03x}  "
                     f"{self.cpuid.part_name}")
        lines.append(f"  revision    {self.cpuid.revision_name}")

        feats = await self.read_features()
        self.features = feats
        decoded = self._decode_features(feats)

        # Headline summary derived from the decoded bitfields. Lets
        # the common case ("does it have an FPU?") stay readable
        # without forcing the verbose register dump.
        if "mvfr0" in decoded:
            mvfr0 = decoded["mvfr0"]
            mvfr1 = decoded.get("mvfr1")
            details = []
            if mvfr0.FPSP != "No":     details.append("SP")
            if mvfr0.FPDP != "No":     details.append("DP")
            if mvfr0.FPSqrt == "Yes":  details.append("sqrt")
            if mvfr0.FPDivide == "Yes": details.append("div")
            if mvfr1 is not None:
                if mvfr1.FMAC == "Implemented": details.append("fmac")
                if mvfr1.FPHP not in ("Not implemented", 0):
                    details.append("HP")
                if mvfr1.MVE != "Not supported":
                    details.append(str(mvfr1.MVE).split(" ")[0])
            lines.append(f"  fpu         "
                         f"{('yes — ' + ', '.join(details)) if details else 'no'}")

        if "isar0" in decoded:
            lines.append(f"  div         "
                         f"{'yes' if decoded['isar0'].Divide else 'no'}")
        if "isar2" in decoded:
            mac = decoded["isar2"].Mult != "MUL"
            lines.append(f"  mac         {'yes' if mac else 'no'}")
        if "pfr1" in decoded:
            lines.append(f"  security    {decoded['pfr1'].Security}")

        if verbose:
            # Full per-register pretty dump. Each register's
            # dump_pretty emits one header line + one line per
            # field, with bit-aligned mask + value columns —
            # invaluable when chasing a chip quirk against the
            # reference manual.
            for name, bf in decoded.items():
                lines.append("")
                bf.dump_pretty(lines.append)
        else:
            rawreg = " ".join(
                f"{k.upper()}=0x{v:08x}"
                for k, v in (
                    ("pfr0", feats.pfr0), ("pfr1", feats.pfr1),
                    ("dfr0", feats.dfr0), ("afr0", feats.afr0),
                    ("mmfr0", feats.mmfr0), ("mmfr1", feats.mmfr1),
                    ("mmfr2", feats.mmfr2), ("mmfr3", feats.mmfr3),
                    ("isar0", feats.isar0), ("isar1", feats.isar1),
                    ("isar2", feats.isar2), ("isar3", feats.isar3),
                    ("isar4", feats.isar4), ("isar5", feats.isar5),
                    ("mvfr0", feats.mvfr0), ("mvfr1", feats.mvfr1),
                    ("mvfr2", feats.mvfr2),
                )
                if v is not None
            )
            if rawreg:
                lines.append(f"  raw         {rawreg}")

        return lines

    @staticmethod
    def _decode_features(feats: "CpuFeatures") -> dict:
        """Wrap each non-None raw value in its Bitfield class.

        Skips registers the core leaves unread (read-faulted or
        not implemented at all — Cortex-M0/M0+ don't carry the
        full feature-ID block, M3/M4 lack CLIDR/CTR/CCSIDR, etc.)."""
        out = {}
        for name, cls in FEATURE_REGISTERS.items():
            raw = getattr(feats, name)
            if raw is None:
                continue
            out[name] = cls(raw)
        return out

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

    # -- Run-control + register access -----------------------------

    async def read_dhcsr(self) -> int:
        return await self.reg_read(self.DHCSR_OFFSET)

    async def read_dfsr(self) -> int:
        return await self.reg_read(self.DFSR_OFFSET)

    async def enable_debug(self) -> None:
        """Set DHCSR.C_DEBUGEN (debug enabled, core not halted)."""
        await self.reg_write(self.DHCSR_OFFSET,
                             self.DHCSR_KEY | self.DHCSR_C_DEBUGEN)

    async def disable_debug(self) -> None:
        """Clear DHCSR.C_DEBUGEN.

        Per ARMv7-M ARM section C1-6, writes that clear C_DEBUGEN
        require two transactions: one with the key + cleared HALT/
        STEP, then one with the whole low half zeroed. Matches
        crobe's enable(False) sequence."""
        await self.reg_write(self.DHCSR_OFFSET, self.DHCSR_KEY)
        await self.reg_write(self.DHCSR_OFFSET, 0)

    async def dhcsr_modify(self, set_bits: int, clear_bits: int) -> None:
        """Read-modify-write the low half of DHCSR. The upper half
        (status bits) is RAZ on write and the key occupies it on
        write; we apply set/clear to the bottom 16 bits only."""
        cur = await self.reg_read(self.DHCSR_OFFSET)
        cur &= 0xFFFF
        cur &= ~(clear_bits & 0xFFFF)
        cur |= (set_bits & 0xFFFF)
        await self.reg_write(self.DHCSR_OFFSET, self.DHCSR_KEY | cur)

    async def demcr_modify(self, set_bits: int, clear_bits: int) -> None:
        cur = await self.reg_read(self.DEMCR_OFFSET)
        cur &= ~clear_bits
        cur |= set_bits
        await self.reg_write(self.DEMCR_OFFSET, cur)

    async def set_reset_catch(self, enabled: bool) -> None:
        if enabled:
            await self.demcr_modify(self.DEMCR_VC_CORERESET, 0)
        else:
            await self.demcr_modify(0, self.DEMCR_VC_CORERESET)

    async def set_hard_error_catch(self, enabled: bool) -> None:
        if enabled:
            await self.demcr_modify(self.DEMCR_VC_HARDERR, 0)
        else:
            await self.demcr_modify(0, self.DEMCR_VC_HARDERR)

    async def cpu_halt(self) -> None:
        """Halt the core via DHCSR.C_HALT.

        Issues the modify; the caller checks DHCSR.S_HALT to verify
        the transition landed — different cores take a variable
        number of cycles to acknowledge."""
        await self.dhcsr_modify(
            self.DHCSR_C_DEBUGEN | self.DHCSR_C_HALT,
            self.DHCSR_C_STEP)

    async def cpu_step(self, *, allow_interrupts: bool = False) -> None:
        """Single-step. Caller must have halted the core first."""
        maskints = 0 if allow_interrupts else self.DHCSR_C_MASKINTS
        base = self.DHCSR_KEY | self.DHCSR_C_DEBUGEN | maskints
        await asyncio.gather(
            self.reg_write(self.DFSR_OFFSET, self.DFSR_CLEAR),
            self.reg_write(self.DHCSR_OFFSET, base | self.DHCSR_C_HALT),
            self.reg_write(self.DHCSR_OFFSET, base | self.DHCSR_C_STEP),
        )

    async def cpu_resume(self, *, allow_interrupts: bool = True) -> None:
        maskints = 0 if allow_interrupts else self.DHCSR_C_MASKINTS
        base = self.DHCSR_KEY | self.DHCSR_C_DEBUGEN | maskints
        await asyncio.gather(
            self.reg_write(self.DFSR_OFFSET, self.DFSR_CLEAR),
            self.reg_write(self.DHCSR_OFFSET, base | self.DHCSR_C_HALT),
            self.reg_write(self.DHCSR_OFFSET, base),
        )

    async def cpu_regs_get(self, register_numbers) -> list[int]:
        """Batch-read core registers by their DCRSR number.

        Per ARMv7-M ARM C1.6.3 the host must observe
        `DHCSR.S_REGRDY=1` between a DCRSR write and the matching
        DCRDR read — otherwise DCRDR is UNKNOWN (returns zero on
        every core we've tested).

        Our sequence per register is `write DCRSR(sel)` → `read
        DHCSR` → `read DCRDR`. The three ops are posted to the
        Batcher in sequence and `gather` waits for the whole
        batch in one flush. The DHCSR read sits between DCRSR and
        DCRDR so the DAP round-trip itself buys the CPU the few
        cycles it needs to set REGRDY — by the time the DHCSR
        transaction lands, the transfer is complete. An assert
        keeps us honest (it has never been observed to fire on
        live silicon; running under `python -O` skips it for
        production)."""
        all_futs = []
        dhcsr_futs = []
        dcrdr_futs = []
        for n in register_numbers:
            all_futs.append(self.reg_write(self.DCRSR_OFFSET, n))
            f_dhcsr = self.reg_read(self.DHCSR_OFFSET)
            dhcsr_futs.append((n, f_dhcsr))
            all_futs.append(f_dhcsr)
            f_dcrdr = self.reg_read(self.DCRDR_OFFSET)
            dcrdr_futs.append(f_dcrdr)
            all_futs.append(f_dcrdr)
        await asyncio.gather(*all_futs)
        for n, f in dhcsr_futs:
            assert f.result() & self.DHCSR_S_REGRDY, (
                f"DCRSR read of reg {n} did not complete "
                f"(DHCSR=0x{f.result():08x}, S_REGRDY=0)")
        return [f.result() for f in dcrdr_futs]

    async def cpu_regs_set(self, pairs) -> None:
        """Batch-write `(register_number, value)` pairs to the core.

        Sequence per register: write value to DCRDR → write
        selector|WRITE to DCRSR → read DHCSR. The DHCSR read
        commits the previous DCRSR transfer's REGRDY status,
        same pattern as cpu_regs_get."""
        all_futs = []
        dhcsr_futs = []
        for n, value in pairs:
            all_futs.append(self.reg_write(self.DCRDR_OFFSET, value))
            all_futs.append(self.reg_write(
                self.DCRSR_OFFSET, n | self.DCRSR_WRITE))
            f_dhcsr = self.reg_read(self.DHCSR_OFFSET)
            dhcsr_futs.append((n, f_dhcsr))
            all_futs.append(f_dhcsr)
        await asyncio.gather(*all_futs)
        for n, f in dhcsr_futs:
            assert f.result() & self.DHCSR_S_REGRDY, (
                f"DCRSR write of reg {n} did not complete "
                f"(DHCSR=0x{f.result():08x}, S_REGRDY=0)")

    async def cpu_reset(self, *, poll_interval: float = 0.01,
                        max_polls: int = 100) -> None:
        """Trigger SYSRESETREQ and wait for the core to clear
        DHCSR.S_RESET_ST.

        Bus access often fails transiently while the system is
        resetting; up to `max_polls` `DpAccessFailure`s are
        absorbed before re-raising. Set `set_reset_catch(True)`
        beforehand if the caller wants the core to come up
        halted."""
        await asyncio.gather(
            self.reg_write(self.DFSR_OFFSET, self.DFSR_CLEAR),
            self.reg_write(self.AIRCR_OFFSET,
                           self.AIRCR_KEY | self.AIRCR_SYSRESETREQ),
        )
        errs = 0
        while True:
            try:
                dhcsr = await self.read_dhcsr()
                errs = 0
                if not (dhcsr & self.DHCSR_S_RESET_ST):
                    return
            except DpAccessFailure:
                errs += 1
                if errs > max_polls:
                    raise
            await asyncio.sleep(poll_interval)
