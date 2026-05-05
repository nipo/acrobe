"""Trace Memory Controller (TMC) — ARM SoC-600 css600_tmc_*.

Captures ATB trace into either an internal SRAM buffer (ETB / ETF)
or system memory (ETR / ETS). The three operating modes share the
same programmer's model and are distinguished by their PIDR0:

    PIDR0 = 0xE9 → css600_tmc_etb (Embedded Trace Buffer)
    PIDR0 = 0xEA → css600_tmc_etf (Embedded Trace FIFO)
    PIDR0 = 0xE8 → css600_tmc_etr / css600_tmc_ets (Embedded Trace
                   Router / Streamer; differentiated at runtime via
                   DEVID, not at registration time)

DEVARCH is RES0 / PRESENT=0 on these components, so registration
goes through ``MemoryMappedComponent.db`` (PartId-keyed) — the
DEVARCH-based registry can't reach them."""

from __future__ import annotations

from .model import MemoryMappedComponent, PartId


class Tmc(MemoryMappedComponent):
    """Trace Memory Controller base. Subclasses pin :attr:`FRIENDLY_NAME`
    and register a specific :class:`PartId`; the register map is shared.

    Only the register *offsets* are declared here — programming the TMC
    (start/stop capture, drain the buffer) is left to callers that
    actually want to use it."""

    # Trace-data and control area (offsets 0x000..0x0FC).
    RSZ        = 0x004  # RAM Size, in 32-bit words
    STS        = 0x00C  # Status (TMCReady, Empty, Full, Triggered, FtEmpty,
                        # MemErr — see SoC-600 TRM 9.18.2 / 9.19.2 / 9.20.2)
    RRD        = 0x010  # RAM Read Data
    RRP        = 0x014  # RAM Read Pointer (ETB/ETF only)
    RWP        = 0x018  # RAM Write Pointer (ETB/ETF only)
    TRG        = 0x01C  # Trigger Counter
    CTL        = 0x020  # Control (TraceCaptEn at bit[0])
    RWD        = 0x024  # RAM Write Data
    MODE       = 0x028  # Operating mode: 0=circular, 1=software FIFO,
                        # 2=hardware FIFO (ETF/ETR — see TRM 9.x.7)
    LBUFLEVEL  = 0x02C  # Latched Buffer Fill Level (ETR/ETS)
    CBUFLEVEL  = 0x030  # Current Buffer Fill Level
    BUFWM      = 0x034  # Buffer Level Water Mark
    RRPHI      = 0x038  # RAM Read Pointer High (ETR, LPAE)
    RWPHI      = 0x03C  # RAM Write Pointer High (ETR, LPAE)

    # ETR/ETS AXI manager interface (only meaningful on those variants;
    # ETB/ETF leave them RAZ/WI).
    AXICTL     = 0x110  # AXI Control
    AXIRR_REQ_LIM = 0x118  # AXI read-request limit

    # ATB interface (formatter side).
    FFSR       = 0x300  # Formatter and Flush Status
    FFCR       = 0x304  # Formatter and Flush Control
    PSCR       = 0x308  # Periodic Synchronization Counter

    # Integration test area.
    ITCTRL     = 0xF00  # Integration Mode Control

    # CTL bits.
    CTL_TRACE_CAPT_EN = 1 << 0

    # STS bits.
    STS_TMC_READY  = 1 << 2
    STS_FT_EMPTY   = 1 << 1
    STS_TRIGGERED  = 1 << 1  # alias kept for callers reading the spec
    STS_FULL       = 1 << 0
    STS_EMPTY      = 1 << 4
    STS_MEM_ERR    = 1 << 5  # ETR/ETS only

    # MODE register values (low 2 bits select the operating mode).
    MODE_CIRCULAR     = 0b00
    MODE_FIFO_SW      = 0b01
    MODE_FIFO_HW      = 0b10


class TmcEtb(Tmc):
    """Embedded Trace Buffer — TMC bound to a dedicated on-chip SRAM."""
    FRIENDLY_NAME = "TMC ETB"


class TmcEtf(Tmc):
    """Embedded Trace FIFO — TMC bound to an on-chip FIFO; can act
    as a buffer or a passthrough formatter for downstream sinks."""
    FRIENDLY_NAME = "TMC ETF"


class TmcEtr(Tmc):
    """Embedded Trace Router / Streamer — TMC bound to an AXI manager
    interface, capturing trace into system memory (ETR) or streaming
    it off-chip (ETS). PIDR is shared between ETR and ETS; DEVID
    distinguishes them at runtime."""
    FRIENDLY_NAME = "TMC ETR"


# ARM SoC-600 PartIds. JEP106 = ARM (bank 4, id 0x3B).
# 12-bit part numbers from PIDR1.PART_1[3:0] || PIDR0.PART_0[7:0].
_ARM = dict(jep106_bank=4, jep106_id=0x3B)

MemoryMappedComponent.db.register(PartId(part_no=0x9E9, **_ARM))(TmcEtb)
MemoryMappedComponent.db.register(PartId(part_no=0x9EA, **_ARM))(TmcEtf)
MemoryMappedComponent.db.register(PartId(part_no=0x9E8, **_ARM))(TmcEtr)
