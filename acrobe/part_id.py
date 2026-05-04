"""Part identifier — JEP106 designer + part number + revision.

Mirrors the layout of a JTAG IDCODE and the PIDR/CIDR fields of a
CoreSight component. Carrying the revision lets us serialize back
to a 32-bit IDCODE; lookups that should ignore revision (matching
silicon revs to a single registration) use :func:`PartId.is_same_part`
or a Db with that eq function.

Ported from crobe — same shape, frozen-dataclass form.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartId:
    """Part identifier — JEP106 designer + 12/16-bit part_no +
    optional 4-bit revision.

    Field widths (from JTAG IDCODE / CoreSight PIDR layouts):
      - jep106_bank : 4 bits (continuation code)
      - jep106_id   : 7 bits (identification code)
      - part_no     : 12 bits via PIDR, 16 bits via JTAG IDCODE / TARGETID
      - revision    : 4 bits

    Equality / hash include all fields. To match a registration that
    deliberately omitted revision, use :meth:`is_same_part` or a Db
    keyed via that comparison."""

    jep106_bank: int
    jep106_id: int
    part_no: int
    revision: int = 0

    @classmethod
    def from_idcode(cls, idcode: int) -> "PartId":
        """Parse a JTAG-style 32-bit IDCODE into its fields.

        Layout: bit[0]=1 (RES1), [7:1]=jep106_id, [11:8]=jep106_bank,
        [27:12]=part_no, [31:28]=revision.

        The bit[0]=1 invariant is a JTAG protocol requirement (the
        capture-DR bit) but isn't enforced here — synthetic test
        idcodes commonly omit it, and the parsing is well-defined
        regardless. ``int(PartId.from_idcode(x))`` round-trips with
        bit 0 forced to 1."""
        idcode = int(idcode)
        return cls(
            jep106_bank=(idcode >> 8) & 0xf,
            jep106_id=(idcode >> 1) & 0x7f,
            part_no=(idcode >> 12) & 0xffff,
            revision=(idcode >> 28) & 0xf,
        )

    def __int__(self) -> int:
        """Pack to a 32-bit IDCODE (the inverse of from_idcode)."""
        return (1
                | (self.jep106_id << 1)
                | (self.jep106_bank << 8)
                | (self.part_no << 12)
                | (self.revision << 28))

    def is_same_part(self, other: "PartId" | None) -> bool:
        """True when the part identity matches, ignoring revision."""
        if other is None:
            return False
        return (self.jep106_bank == other.jep106_bank
                and self.jep106_id == other.jep106_id
                and self.part_no == other.part_no)

    def drop_revision(self) -> "PartId":
        """Return a copy with revision = 0 (canonical form for
        registry keys that should match across silicon revs)."""
        return PartId(self.jep106_bank, self.jep106_id, self.part_no, 0)

    @property
    def manufacturer_name(self) -> str:
        """Look up the JEP106 manufacturer name. Late import to keep
        the bulk JEDEC table out of every module that touches PartId."""
        from . import jep106
        return jep106.name_get(self.jep106_bank, self.jep106_id)

    def pretty(self) -> str:
        """Human-readable form, e.g.
        ``"0x4ba00477 (ARM Ltd., 0xba00, r0)"``."""
        return (f"0x{int(self):08x} "
                f"({self.manufacturer_name}, 0x{self.part_no:04x}, "
                f"r{self.revision})")

    def __str__(self) -> str:
        return self.pretty()
