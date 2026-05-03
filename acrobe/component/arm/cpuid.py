"""ARM CPUID register decoder.

The ARM CPUID register (M-profile: SCS+0xD00, A/R-profile: MIDR_EL1)
identifies the CPU core: implementer, ISA architecture, part number,
and silicon revision. This module exposes a frozen dataclass that
parses the 32-bit value into its fields and looks up human-readable
names for the implementer + part number."""

from __future__ import annotations

from dataclasses import dataclass


# JEP106-style implementer codes (the "DESIGNER" byte). Same encoding
# the JEP106 helper uses but here it's a flat 8-bit value, no
# continuation byte — the CPUID register has only one byte for it.
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


# ARM-implementer part numbers (12-bit PARTNO field). Both Cortex-M
# and Cortex-A/R end up in the same registry here. The high nibble
# is a rough family selector — 0xC = Cortex-M0..M7 and Cortex-A
# series, 0xD = Cortex-M23/33/55/85 + Cortex-A* later generations,
# 0xR for older R-series, etc. We just spell each one out.
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
    # Cortex-A (a sample — extend as needed)
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
        bits 31..24: IMPLEMENTER (8-bit JEP106-style designer)
        bits 23..20: VARIANT     (silicon major revision, "rN")
        bits 19..16: ARCHITECTURE (0xC = ARMv6-M, 0xF = ARMv7-M /
                                  ARMv8-M / ARMv7-A; effectively
                                  ignored at this layer — the part
                                  number is the canonical identifier)
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

    def pretty(self) -> str:
        """One-line "<implementer> <part> rXpY" summary."""
        return f"{self.implementer_name} {self.part_name} {self.revision_name}"

    def __str__(self) -> str:
        return self.pretty()
