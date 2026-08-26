"""ATB Replicator — ARM SoC-600 css600_atbreplicator_prog.

Splits a single ATB trace stream onto two transmitter ports, with
optional per-trace-ID filtering: ``IDFILT0`` controls which trace IDs
reach port 0, ``IDFILT1`` controls port 1. Each filter is a 32-bit
bitmap where bit ``n`` covers IDs in the 0x10 range starting at
``n*0x10`` (bit 0 → IDs 0x00..0x0F, bit 1 → 0x10..0x1F, ...; bit 31
→ 0x70..0x7F).

DEVARCH is RES0 / PRESENT=0 on this component (TRM 9.11.11), so it
falls through to the PartId-keyed registry. PartId 0x9EC is the
*programmable* variant; the non-programmable ATB Replicator has no
register file (it's wired) and never appears in a ROM Table."""

from __future__ import annotations

from .model import MemoryMappedComponent, PartId

# ARM SoC-600 css600_atbreplicator_prog: PIDR1.PART_1=0x9, PIDR0.PART_0=0xEC.
@MemoryMappedComponent.db.register(PartId.from_idcode(0x9EC477))
class AtbReplicator(MemoryMappedComponent):
    """Programmable ATB replicator (1-to-2 splitter with ID filters)."""

    FRIENDLY_NAME = "ATB Replicator"

    # ID-filter bitmaps. Each bit covers a contiguous block of 16 IDs:
    # bit 0 = IDs 0x00..0x0F, ..., bit 7 = IDs 0x70..0x7F. ID space
    # used by ATB stops at 0x7F, so bits[31:8] are RES0.
    IDFILT0    = 0x000  # Transmitter port 0 filter (1 = discard)
    IDFILT1    = 0x004  # Transmitter port 1 filter (1 = discard)

    # Integration test area.
    ITATBCTRL  = 0xEF8  # Integration Mode ATB Control
    ITCTRL     = 0xF00  # Integration Mode Control
