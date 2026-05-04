"""Renesas-designed CoreSight components.

These appear inside the APB-debug ROM Table on Renesas v8-M SoCs
(e.g. RA4M2 paired with a Cortex-M85). JEP106 designer is Renesas
Electronics (bank 4, id 0x23) — distinct from ARM's 4:0x3B."""

from ..arm.coresight.model import MemoryMappedComponent, PartId


_RENESAS = dict(jep106_bank=4, jep106_id=0x23)


class OcdReg(MemoryMappedComponent):
    """Renesas On-Chip Debug Register block. Vendor-specific debug
    interface; programmer's model is documented per-MCU in the
    Renesas user's manual."""

    FRIENDLY_NAME = "Renesas OCDREG"


MemoryMappedComponent.db.register(
    PartId(part_no=0x004, **_RENESAS)
)(OcdReg)
