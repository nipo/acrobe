"""ARM CPUID + feature-ID register decoders.

Two layers of decode here:

1. :class:`Cpuid` — frozen dataclass that parses the CPUID register
   itself (implementer / variant / architecture / part_no /
   revision) and looks up human-readable names for the implementer
   and the ARM Cortex-M / Cortex-A / Cortex-R part numbers.
2. :class:`PFR0`, :class:`MMFR1`, :class:`ISAR3`, … — declarative
   bitfield classes (built on :mod:`acrobe.bitfield`) for every
   M-profile feature-ID register the SCS exposes. Each one is
   instantiated with a 32-bit raw value, then dumped via
   :meth:`Bitfield.dump_pretty` for human consumption or queried
   field-by-field for programmatic feature detection.

Field definitions ported from crobe ``crobe/component/arm/cpuid.py``
and updated with the bits ARMv8-M / Helium-equipped cores (M55, M85)
added on top of ARMv7-M."""

from __future__ import annotations

from dataclasses import dataclass

from ...bitfield import Bitfield, BooleanField, Field, MappingField


# --- CPUID register (SCS+0xD00) ------------------------------------

_IMPLEMENTER_NAMES = {
    0x41: "ARM Ltd.",
    0x42: "Broadcom",
    0x43: "Cavium",
    0x44: "DEC",
    0x46: "Fujitsu",
    0x49: "Infineon",
    0x4D: "Motorola/Freescale",
    0x4E: "NVIDIA",
    0x50: "Applied Micro",
    0x51: "Qualcomm",
    0x53: "Samsung",
    0x54: "Texas Instruments",
    0x56: "Marvell",
    0x61: "Apple",
    0x66: "Faraday",
    0x68: "HiSilicon",
    0x69: "Intel",
    0xC0: "Ampere Computing",
}

_ARM_PART_NAMES = {
    # Cortex-M
    0xC20: "Cortex-M0",
    0xC21: "Cortex-M1",
    0xC23: "Cortex-M3",
    0xC24: "Cortex-M4",
    0xC27: "Cortex-M7",
    0xC60: "Cortex-M0+",
    0xD20: "Cortex-M23",
    0xD21: "Cortex-M33",
    0xD22: "Cortex-M55",
    0xD23: "Cortex-M85",
    # Cortex-R
    0xC14: "Cortex-R4",
    0xC15: "Cortex-R5",
    0xC17: "Cortex-R7",
    0xC18: "Cortex-R8",
    0xD13: "Cortex-R52",
    # Cortex-A (sample — extend as needed)
    0xC05: "Cortex-A5",
    0xC07: "Cortex-A7",
    0xC08: "Cortex-A8",
    0xC09: "Cortex-A9",
    0xC0D: "Cortex-A12",
    0xC0E: "Cortex-A17",
    0xC0F: "Cortex-A15",
    0xD03: "Cortex-A53",
    0xD04: "Cortex-A35",
    0xD05: "Cortex-A55",
    0xD07: "Cortex-A57",
    0xD08: "Cortex-A72",
    0xD09: "Cortex-A73",
    0xD0A: "Cortex-A75",
    0xD0B: "Cortex-A76",
    0xD0C: "Neoverse N1",
    0xD0D: "Cortex-A77",
}


@dataclass(frozen=True)
class Cpuid:
    """Decoded CPUID register value.

    Layout per ARM ARM:
        bits 31..24: IMPLEMENTER  (8-bit JEP106-style designer)
        bits 23..20: VARIANT      (silicon major revision, "rN")
        bits 19..16: ARCHITECTURE (0xC = ARMv6-M,
                                   0xF = ARMv7-M / ARMv8-M / ARMv7-A)
        bits 15..4:  PARTNO       (12-bit core ID)
        bits 3..0:   REVISION     (silicon minor revision, "pN")
    """

    raw: int
    implementer: int
    variant: int
    architecture: int
    part_no: int
    revision: int

    @classmethod
    def from_raw(cls, value: int) -> "Cpuid":
        v = value & 0xFFFFFFFF
        return cls(
            raw=v,
            implementer=(v >> 24) & 0xFF,
            variant=(v >> 20) & 0xF,
            architecture=(v >> 16) & 0xF,
            part_no=(v >> 4) & 0xFFF,
            revision=v & 0xF,
        )

    @property
    def implementer_name(self) -> str:
        return _IMPLEMENTER_NAMES.get(
            self.implementer, f"unknown(0x{self.implementer:02x})")

    @property
    def part_name(self) -> str:
        if self.implementer == 0x41 and self.part_no in _ARM_PART_NAMES:
            return _ARM_PART_NAMES[self.part_no]
        return f"part_0x{self.part_no:03x}"

    @property
    def revision_name(self) -> str:
        return f"r{self.variant}p{self.revision}"

    @property
    def architecture_name(self) -> str:
        return {
            0xC: "ARMv6-M",
            0xF: "ARMv7-M / ARMv8-M",
        }.get(self.architecture, f"unknown(0x{self.architecture:x})")

    def pretty(self) -> str:
        return f"{self.implementer_name} {self.part_name} {self.revision_name}"

    def __str__(self) -> str:
        return self.pretty()


# --- Feature-ID register bitfields ---------------------------------
#
# Each class below is a Bitfield instantiated with a raw 32-bit
# value (the read result from the corresponding SCS register).
# Field names + value mappings follow the M-profile architecture
# reference manual, with v8-M / Helium additions called out.


class PFR0(Bitfield):
    """Processor Feature 0 — ISA / state support."""

    RAS = MappingField(
        28, 4, {0: "None", 3: "V1"},
        doc="Reliability, Availability, Serviceability extension")
    DIT = MappingField(
        24, 4, {0: "None", 1: "Implemented"},
        doc="Data-Independent Timing instructions")
    ThumbEE = MappingField(
        12, 4, {0: "None", 3: "Thumb-2"})
    Acceleration = MappingField(
        8, 4, {0: "None", 1: "Software", 3: "Thumb-2"})
    State1 = MappingField(
        4, 4, {0: "None", 1: "Thumb", 3: "Thumb-2"},
        doc="State 1 ISA")
    State0 = MappingField(
        0, 4, {0: "None", 1: "ARM"},
        doc="State 0 ISA")


class PFR1(Bitfield):
    """Processor Feature 1 — programmer's model + extensions."""

    CSV3 = MappingField(
        24, 4, {0: "None", 1: "Implemented"},
        doc="Cache Speculation Variant 3 mitigation")
    MProgMod = MappingField(
        8, 4, {0: "None", 2: "Two-stack"},
        doc="M-profile programmers' model")
    Security = MappingField(
        4, 4, {0: "None", 1: "Implemented",
               3: "Implemented with state handling (v8-M)"},
        doc="Security Extension")
    ARMv4_Prog_Model = MappingField(0, 4, {0: "None", 1: "ARMv4"})


class DFR0(Bitfield):
    """Debug Feature 0."""

    UDE = MappingField(
        28, 4, {0: "None", 1: "Implemented"},
        doc="Unprivileged Debug Extension (v8.1-M)")
    MProfDbg = MappingField(
        20, 4, {0: "None", 1: "Memory mapped",
                2: "Halting supported"},
        doc="M-profile debug architecture")
    Trace_MM = MappingField(
        16, 4, {0: "None", 1: "Memory mapped"},
        doc="Memory-mapped trace model")
    Trace_Cop = MappingField(12, 4, {0: "None"},
                             doc="Coprocessor-based trace model")
    Core_MM = MappingField(8, 4, {0: "None", 1: "ARMv4 MM",
                                  4: "ARMv7 MM"},
                           doc="Memory-mapped core debug model")
    Secure_Dbg = MappingField(4, 4, {0: "None"},
                              doc="Secure debug")
    Core_Cop = MappingField(0, 4, {0: "None"},
                            doc="Coprocessor-based core debug model")


class AFR0(Bitfield):
    """Auxiliary Feature 0 — implementation defined."""

    ImpDef0 = Field(0, 4, doc="Implementation defined")
    ImpDef1 = Field(4, 4, doc="Implementation defined")
    ImpDef2 = Field(8, 4, doc="Implementation defined")
    ImpDef3 = Field(12, 4, doc="Implementation defined")


class MMFR0(Bitfield):
    """Memory Model & Memory-Mapping Feature 0."""

    InnerShr = Field(28, 4, doc="Innermost Shareability")
    FCSE = MappingField(24, 4, {0: "None"})
    AuxReg = MappingField(
        20, 4, {0: "None", 1: "ACR only", 2: "AIFSR + ADFSR"},
        doc="Auxiliary control registers")
    TCM = MappingField(
        16, 4, {0: "None", 1: "Implementation-defined control"},
        doc="Tightly Coupled Memory")
    ShareLvl = MappingField(
        12, 4, {0: "One level", 1: "Two levels"},
        doc="Shareability levels")
    OuterShr = MappingField(
        8, 4, {0: "Non-cacheable", 1: "Hardware coherency",
               15: "Ignored"},
        doc="Outermost Shareability")
    PMSA = MappingField(
        4, 4, {0: "Not supported", 3: "PMSAv7 with subregions",
               4: "PMSAv8 (v8-M baseline)"},
        doc="Protected Memory System Architecture")
    VMSA = MappingField(0, 4, {0: "Not supported"},
                        doc="Virtual Memory System Architecture")


class MMFR1(Bitfield):
    """Memory Model Feature 1 — branch predictor + cache topology."""

    Branch_Predictor = MappingField(28, 4, {0: "None"})
    L1_Test_Clean = MappingField(24, 4, {0: "None"})
    L1_Unified = MappingField(20, 4, {0: "None"})
    L1_Harvard = MappingField(16, 4, {0: "None"})
    L1_SetWay_Unified = MappingField(12, 4, {0: "None"})
    L1_SetWay_Harvard = MappingField(8, 4, {0: "None"})
    L1_MVA_Unified = MappingField(4, 4, {0: "None"})
    L1_MVA_Harvard = MappingField(0, 4, {0: "None"})


class MMFR2(Bitfield):
    """Memory Model Feature 2 — barriers + cache + TLB ops."""

    HW_Access_Flag = MappingField(28, 4, {0: "Not supported"})
    WFI_Stall = MappingField(24, 4,
                             {0: "Not supported", 1: "Supported"})
    Barriers = MappingField(20, 4, {0: "Not supported",
                                    2: "DSB, ISB, DMB"})
    TLB_Unified = MappingField(16, 4, {0: "Not supported"})
    TLB_Harvard = MappingField(12, 4, {0: "Not supported"})
    L1_Cache = MappingField(8, 4, {0: "Not supported"})
    L1_Bg_Prefetch = MappingField(4, 4, {0: "Not supported"})
    L1_Fg_Prefetch = MappingField(0, 4, {0: "Not supported"})


class MMFR3(Bitfield):
    """Memory Model Feature 3 — supersection / coherency / cache mgmt."""

    Supersection = MappingField(28, 4, {0: "Not supported"})
    Coherent_Walk = MappingField(20, 4, {0: "Not supported"})
    Maint_Bcast = MappingField(12, 4, {0: "Not supported"})
    BPMaint = MappingField(
        8, 4, {0: "Not supported", 1: "Invalidate all",
               2: "Invalidate by MVA"},
        doc="Branch predictor maintenance")
    CMaintSW = MappingField(
        4, 4, {0: "Not supported", 1: "Invalidate/clean"},
        doc="Cache maintenance for set/way")
    CMaintVA = MappingField(
        0, 4, {0: "Not supported", 1: "Invalidate/clean"},
        doc="Cache maintenance by address")


class ISAR0(Bitfield):
    """Instruction Set Attribute 0."""

    Divide = MappingField(24, 4, {0: "", 1: "SDIV, UDIV"})
    Debug = MappingField(20, 4, {0: "", 1: "BKPT"})
    Coproc = MappingField(
        16, 4,
        {0: "",
         1: "CDP, LDC, MCR, MRC, STC",
         2: "+ CDP2, LDC2, MCR2, MRC2, STC2",
         3: "+ MCRR, MRRC",
         4: "+ MCRR2, MRRC2"})
    CmpBranch = MappingField(
        12, 4,
        {0: "",
         1: "CBNZ, CBZ",
         3: "CBNZ, CBZ + non-predicated low-overhead loops (v8.1-M)"})
    Bitfield_Insns = MappingField(
        8, 4, {0: "", 1: "BFC, BFI, SBFX, UBFX"})
    Bitcount = MappingField(4, 4, {0: "", 1: "CLZ"})
    Atomics = MappingField(0, 4, {0: "", 1: "SWP, SWPB"})


class ISAR1(Bitfield):
    """Instruction Set Attribute 1."""

    Jazelle = MappingField(28, 4, {0: "", 1: "BXJ"})
    Interwork = MappingField(
        24, 4,
        {0: "", 1: "BX", 2: "BX, BLX",
         3: "BX, BLX + dp insts"})
    Immediate = MappingField(
        20, 4, {0: "", 1: "ADDW, MOVW, MOVT, SUBW"})
    If_Then = MappingField(16, 4, {0: "", 1: "IT"})
    Extend = MappingField(
        12, 4,
        {0: "",
         1: "SXTB, SXTH, UXTB, UXTH",
         2: "+ {S,U}XTAB[16], {S,U}XTAH, {S,U}XTB16"})
    Except2 = MappingField(8, 4, {0: "", 1: "RFE, SRS, CPS"})
    Except1 = MappingField(4, 4,
                           {0: "", 1: "LDM (exc), STM (user)"})
    Endian = MappingField(0, 4, {0: "", 1: "SETEND"})


class ISAR2(Bitfield):
    """Instruction Set Attribute 2 — multiply / load-store."""

    Reversal = MappingField(
        28, 4,
        {0: "",
         1: "REV, REV16, REVSH",
         2: "REV, REV16, REVSH, RBIT"})
    PSR = MappingField(24, 4, {0: "", 1: "MSR, MRS"})
    MultU = MappingField(
        20, 4, {0: "", 1: "UMULL, UMLAL", 2: "+ UMAAL"})
    MultS = MappingField(
        16, 4,
        {0: "", 1: "SMULL, SMLAL",
         2: "+ SM[L]A[B/T][B/T], SMLAW[B/T], SMUL[B/T][B/T], SMULW[B/T]",
         3: "+ SM[L]AD, SM[L]ALD, SM[L]SD, SM[M]LA[R], SM[M]LS[R], SM[M]UL[R], SMUAD, SMUSD"})
    Mult = MappingField(
        12, 4, {0: "MUL", 1: "MUL, MLA", 2: "MUL, MLA, MLS"})
    MultiAccessInt = MappingField(
        8, 4,
        {0: "", 1: "LDM, STM restartable",
         2: "LDM, STM continuable"})
    MemHint = MappingField(
        4, 4,
        {0: "", 1: "PLD", 2: "PLD",
         3: "PLD, PLI", 4: "PLD, PLI, PLDW"})
    LoadStore = MappingField(
        0, 4,
        {0: "", 1: "LDRD, STRD",
         2: "Load-Acquire, Store-Release, Exclusives"})


class ISAR3(Bitfield):
    """Instruction Set Attribute 3."""

    ThumbEE = MappingField(28, 4, {0: ""})
    TrueNOP = MappingField(24, 4, {0: "", 1: "NOP"})
    ThumbCopy = MappingField(20, 4, {0: "", 1: "MOV.t1"})
    TabBranch = MappingField(16, 4, {0: "", 1: "TBB, TBH"})
    SynchPrim = MappingField(
        12, 4,
        {0: "", 2: "LDREX[BH], STREX[BH], CLREX"})
    SVC = MappingField(8, 4, {0: "", 1: "SVC"})
    SIMD = MappingField(
        4, 4,
        {0: "", 1: "SSAT, USAT",
         3: "SSAT, USAT + GE-bits, DSP only"})
    Saturate = MappingField(
        0, 4, {0: "", 1: "QADD, QDADD, QDSUB, QSUB"})


class ISAR4(Bitfield):
    """Instruction Set Attribute 4."""

    SWP_frac = MappingField(28, 4, {0: ""})
    PSR_M = MappingField(
        24, 4, {0: "", 1: "CPS, MRS, MSR"})
    SynchPrim_frac = MappingField(
        20, 4, {0: "", 3: "(LDR,STR,CLR)EX[BH]"})
    Barrier = MappingField(
        16, 4, {0: "", 1: "DMB, DSB, ISB"})
    Writeback = MappingField(
        8, 4,
        {0: "STM, LDM, PUSH, POP only",
         1: "All v7-M instructions"})
    WithShifts = MappingField(
        4, 4,
        {0: "MOV and shift only",
         1: "MOV, shift, load, store (lsl 0..3)",
         3: "MOV, shift, load, store (lsl 0..3 + constants)",
         4: "Full"})
    Unpriv = MappingField(
        0, 4,
        {0: "",
         1: "LDRBT, LDRT, STRBT, STRT",
         2: "LDR{SB,B,SH,H}T, STR{B,H}T"})


class ISAR5(Bitfield):
    """Instruction Set Attribute 5 — ARMv8/v8.1-M crypto + PACBTI.

    Mostly zero on classic Cortex-M3/M4/M7. ARMv8.1-M cores (M55,
    M85) populate PACBTI; future Helium-Crypto chips use the
    SHA / AES / CRC32 fields too."""

    VCMA = MappingField(28, 4, {0: "Not implemented", 1: "VCMA, VCADD"},
                        doc="Complex-number SIMD")
    RDM = MappingField(24, 4, {0: "Not implemented",
                               1: "VQRDMLAH, VQRDMLSH"},
                       doc="Rounding double multiply add/sub")
    PACBTI = MappingField(
        20, 4,
        {0: "Not implemented",
         1: "QARMA5",
         2: "Implementation defined",
         4: "QARMA3"},
        doc="Pointer Authentication / Branch Target Identification")
    CRC32 = MappingField(16, 4, {0: "Not implemented",
                                 1: "CRC32B/H/W/X"},
                         doc="CRC32 instructions")
    SHA2 = MappingField(12, 4, {0: "Not implemented",
                                1: "SHA256H/H2/SU0/SU1"},
                        doc="SHA2 acceleration")
    SHA1 = MappingField(8, 4, {0: "Not implemented",
                               1: "SHA1[CHMP], SHA1SU0/1"},
                        doc="SHA1 acceleration")
    AES = MappingField(4, 4, {0: "Not implemented",
                              1: "AESE, AESD, AESMC, AESIMC",
                              2: "+ PMULL"},
                       doc="AES acceleration")
    SEVL = MappingField(0, 4, {0: "Not implemented",
                               1: "SEVL"},
                        doc="Send Event Local")


class MVFR0(Bitfield):
    """Media and VFP Feature 0."""

    FPRound = MappingField(28, 4, {0: "Default only", 1: "All"})
    Short_vectors = MappingField(24, 4, {0: "No"})
    FPSqrt = MappingField(20, 4, {0: "No", 1: "Yes"})
    FPDivide = MappingField(16, 4, {0: "No", 1: "Yes"})
    FP_Exception_Trapping = MappingField(12, 4, {0: "No"})
    FPDP = MappingField(8, 4, {0: "No", 1: "Yes"},
                        doc="Double-precision FP")
    FPSP = MappingField(4, 4, {0: "No", 1: "Yes",
                               2: "Yes (with restrictions)"},
                        doc="Single-precision FP")
    SIMDReg = MappingField(0, 4, {0: "Not implemented",
                                  1: "16x64 bits",
                                  2: "32x64 bits"})


class MVFR1(Bitfield):
    """Media and VFP Feature 1 — fused MAC, half-precision, MVE."""

    FMAC = MappingField(
        28, 4, {0: "Not implemented", 1: "Implemented"},
        doc="Fused multiply-accumulate (VFMA, VFMS, …)")
    FPHP = MappingField(
        24, 4,
        {0: "Not implemented",
         1: "HP-SP conversion only",
         2: "HP-SP-DP conversion",
         3: "Full half-precision"},
        doc="Half-precision FP")
    FP16 = MappingField(20, 4, {0: "Not implemented",
                                1: "Implemented"},
                        doc="Half-precision data-processing")
    MVE = MappingField(
        8, 4,
        {0: "Not supported",
         1: "MVE-int (Helium integer)",
         2: "MVE-fp (Helium FP)"},
        doc="M-profile Vector Extension (Helium, ARMv8.1-M)")
    FPDNaN = MappingField(
        4, 4, {0: "None", 1: "Supported"},
        doc="FP NaN propagation")
    FPFtZ = MappingField(
        0, 4,
        {0: "Not supported",
         1: "Full denormalized support"},
        doc="FP Flush-to-Zero")


class MVFR2(Bitfield):
    """Media and VFP Feature 2."""

    FPMisc = MappingField(
        4, 4, {0: "No", 4: "Min, max, rounding"},
        doc="Miscellaneous FP features")
    SIMDMisc = MappingField(0, 4, {0: "No"})


class CLIDR(Bitfield):
    """Cache Level ID — Cortex-M7 / ARMv8-M."""

    _CACHE_MODE = {
        0: "None",
        1: "I",
        2: "D",
        3: "Separate I+D",
        4: "Unified I+D",
    }
    Ctype1 = MappingField(0, 3, _CACHE_MODE)
    Ctype2 = MappingField(3, 3, _CACHE_MODE)
    Ctype3 = MappingField(6, 3, _CACHE_MODE)
    Ctype4 = MappingField(9, 3, _CACHE_MODE)
    Ctype5 = MappingField(12, 3, _CACHE_MODE)
    Ctype6 = MappingField(15, 3, _CACHE_MODE)
    Ctype7 = MappingField(18, 3, _CACHE_MODE)
    LoUIS = Field(21, 3, offset=1,
                  doc="Level of Unification Inner Shareable")
    LoC = Field(24, 3, offset=1, doc="Level of Coherence")
    LoUU = Field(27, 3, offset=1,
                 doc="Level of Unification Uniprocessor")
    ICB = Field(30, 2,
                doc="Inner Cache Boundary (highest inner cache level)")


class CCSIDR(Bitfield):
    """Cache Size ID — current cache (selected via CSSELR)."""

    WT = BooleanField(31, doc="Write-Through supported")
    WB = BooleanField(30, doc="Write-Back supported")
    RA = BooleanField(29, doc="Read-Allocation supported")
    WA = BooleanField(28, doc="Write-Allocation supported")
    NumSets = Field(13, 15, offset=1, doc="(NumSets - 1)")
    Associativity = Field(3, 10, offset=1, doc="(Assoc - 1)")
    LineSize = Field(0, 3, offset=2,
                     doc="LineSize = 2^(LineSize + 4) bytes")


class CTR(Bitfield):
    """Cache Type Register — overall cache topology."""

    Format = Field(29, 3, doc="ARMv7+ format = 4")
    CWG = Field(24, 4, doc="Cache Write-back Granule (log2 words)")
    ERG = Field(20, 4, doc="Exclusives Reservation Granule")
    DminLine = Field(16, 4,
                     doc="Smallest D-cache line (log2 words)")
    L1Ip = MappingField(
        14, 2,
        {0: "VPIPT", 1: "AIVIVT", 2: "VIPT", 3: "PIPT"},
        doc="L1 instruction cache policy")
    IminLine = Field(0, 4,
                     doc="Smallest I-cache line (log2 words)")


# Register-name → Bitfield-class mapping. Keeps the dump code in
# Scs.dump_cpu trivial: look up the class by attribute name and
# instantiate with the raw value.
FEATURE_REGISTERS = {
    "pfr0": PFR0,
    "pfr1": PFR1,
    "dfr0": DFR0,
    "afr0": AFR0,
    "mmfr0": MMFR0,
    "mmfr1": MMFR1,
    "mmfr2": MMFR2,
    "mmfr3": MMFR3,
    "isar0": ISAR0,
    "isar1": ISAR1,
    "isar2": ISAR2,
    "isar3": ISAR3,
    "isar4": ISAR4,
    "isar5": ISAR5,
    "mvfr0": MVFR0,
    "mvfr1": MVFR1,
    "mvfr2": MVFR2,
    "clidr": CLIDR,
    "ctr":   CTR,
    "ccsidr": CCSIDR,
}
