"""ARM Access Port (AP) base class.

An AP is a Node that translates DP-level register accesses into
operations on a downstream system. The base ``Ap`` here provides
the IDR-based identification machinery; concrete AP types (MEM-AP,
JTAG-AP, vendor-specific) register against ``Ap.db`` keyed on their
IDR signature and inherit register-access plumbing.

ADIv6 view: an AP is identified by its base address in the DP's
system address space. ADIv5 chips fall out as the special case
``base = apsel << 24``.

The IDR layout (ADI v5 and ADI v6 both):

    31      28 27          17 16    13 12    8 7    4 3    0
   |REVISION| DESIGNER (JEP)| CLASS | RES0 |VARIANT| TYPE |

The ``Ap.db`` equality function masks REVISION (bits 31:28) so
revision-bumped silicon hits the same registration.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...db import Db, NoMatch
from ...node import Node
from . import dp as dpmod


@dataclass(frozen=True, slots=True)
class ApIdr:
    """Decoded AP Identification Register (offset 0xFC).

    Layout per the ADI spec:

        31      28 27          17 16    13 12    8 7    4 3    0
       |REVISION| DESIGNER (JEP)| CLASS | RES0 |VARIANT| TYPE |

    Equality / hash include all fields. Use :meth:`is_same_ap_type`
    or a Db keyed via that comparison to ignore revision + variant
    so a single registration covers every silicon rev + minor variant
    of a given (designer, class, type) combination."""

    jep106_bank: int   # 4 bits — JEP106 continuation
    jep106_id: int     # 7 bits — JEP106 identification code
    klass: int         # CLASS, 4 bits
    variant: int       # VARIANT, 4 bits
    type: int          # TYPE, 4 bits
    revision: int      # REVISION, 4 bits

    @classmethod
    def from_idr(cls, idr: int) -> "ApIdr":
        """Parse a 32-bit IDR value into the typed form."""
        idr = int(idr)
        designer = (idr >> 17) & 0x7FF
        return cls(
            jep106_bank=(designer >> 7) & 0xF,
            jep106_id=designer & 0x7F,
            klass=(idr >> 13) & 0xF,
            variant=(idr >> 4) & 0xF,
            type=idr & 0xF,
            revision=(idr >> 28) & 0xF,
        )

    def __int__(self) -> int:
        """Pack back to a 32-bit IDR (the inverse of from_idr)."""
        designer = (self.jep106_bank << 7) | self.jep106_id
        return ((self.revision << 28)
                | (designer << 17)
                | (self.klass << 13)
                | (self.variant << 4)
                | self.type)

    def is_same_ap_type(self, other: "ApIdr") -> bool:
        """True when DESIGNER + CLASS + TYPE match — masks out
        REVISION and VARIANT. Lets one registration cover every
        silicon rev + minor variant of an AP type."""
        return (self.jep106_bank == other.jep106_bank
                and self.jep106_id == other.jep106_id
                and self.klass == other.klass
                and self.type == other.type)

    @property
    def manufacturer_name(self) -> str:
        from ...jep106 import name_get
        return name_get(self.jep106_bank, self.jep106_id)

    def pretty(self) -> str:
        return (f"0x{int(self):08x} "
                f"({self.manufacturer_name}, "
                f"class=0x{self.klass:x} type=0x{self.type:x}, "
                f"r{self.revision})")

    def __str__(self) -> str:
        return self.pretty()


def _idr_eq(key, lookup) -> bool:
    """Match AP IDRs ignoring REVISION + VARIANT, accepting either
    raw 32-bit IDR ints or :class:`ApIdr` instances on either side."""
    if isinstance(key, int):
        key = ApIdr.from_idr(key)
    if isinstance(lookup, int):
        lookup = ApIdr.from_idr(lookup)
    return key.is_same_ap_type(lookup)


class Ap(Node):
    """Access Port base. Routes register reads/writes through the
    parent DP. Concrete subclasses register against ``Ap.db`` keyed
    on their IDR signature."""

    # Register offsets common to all ADI APs.
    IDR  = 0xFC   # Identification Register
    IDR1 = 0xFD0  # ADIv6 extended identification (RES0 on ADIv5)

    db: Db = Db("AP IDR (ApIdr)", eq_func=_idr_eq)

    # CLASS field decoding (IDR[16:13]).
    CLASS_NONE      = 0b0000  # Not an MEM-AP / JTAG-AP / etc. — see TYPE
    CLASS_COM_AP    = 0b0001  # Communications AP (vendor-specific)
    CLASS_MEM_AP    = 0b1000  # Memory AP (AHB / APB / AXI)

    def __init__(self, dp: dpmod.Dp, base: int, idr: int = 0,
                 name: str | None = None):
        if name is None:
            # ADIv5-style AP at apsel << 24: name "ap{apsel}".
            # ADIv6 AP at arbitrary address: name "ap@{base:x}".
            if base & 0x00FFFFFF == 0 and base != 0:
                name = f"ap{base >> 24}"
            elif base == 0:
                name = "ap0"
            else:
                name = f"ap@{base:08x}"
        super().__init__(name)
        self._dp = dp
        self.base = base
        self.idr = idr

    # -- IDR field accessors (cached from idr value) ----------------

    @property
    def revision(self) -> int:
        return (self.idr >> 28) & 0xf

    @property
    def designer(self) -> int:
        return (self.idr >> 17) & 0x7ff

    @property
    def klass(self) -> int:
        return (self.idr >> 13) & 0xf

    @property
    def variant(self) -> int:
        return (self.idr >> 4) & 0xf

    @property
    def type(self) -> int:
        return self.idr & 0xf

    # -- Register access via the parent DP --------------------------

    def reg_read(self, addr: int):
        """Post an AP register read. Returns Future -> int."""
        return self._dp.post(dpmod.ApRead(addr=self.base + addr))

    def reg_write(self, addr: int, data: int):
        """Post an AP register write. Returns Future -> None."""
        return self._dp.post(dpmod.ApWrite(addr=self.base + addr, data=data))

    # -- Discovery --------------------------------------------------

    @classmethod
    async def discover(cls, dp: dpmod.Dp, base: int) -> "Ap | None":
        """Read IDR at the given AP base address; return an Ap of the
        most-specific registered subclass, or ``None`` if no AP is
        present (IDR = 0).

        Caller is responsible for adding the returned Ap to the DP's
        Node tree (usually via ``dp.child_add(ap)``).

        Failure modes:
          * IDR read raises ``DpAccessFailure`` → logged at protocol
            level, returns ``None`` (treat as "no AP here" — keep
            walking sibling APSELs).
          * Registered handler ``__init__`` raises → logged at warning
            level, falls back to a generic ``Ap`` so the AP still
            surfaces in enumeration with its IDR."""
        idr_future = dp.post(dpmod.ApRead(addr=base + cls.IDR))
        try:
            idr = await idr_future
        except dpmod.DpAccessFailure as exc:
            dp.logger.protocol(
                "AP IDR read at 0x%08x failed: %s", base, exc)
            return None
        if idr == 0:
            return None
        try:
            return cls.db.call(ApIdr.from_idr(idr),
                               dp=dp, base=base, idr=idr)
        except NoMatch:
            return cls(dp=dp, base=base, idr=idr)
        except Exception as exc:
            dp.logger.warning(
                "AP at 0x%08x (idr=0x%08x): registered handler raised "
                "%s during construction (falling back to generic Ap): %s",
                base, idr, type(exc).__name__, exc, exc_info=True)
            return cls(dp=dp, base=base, idr=idr)

    def __repr__(self):
        return (f"<{type(self).__name__} {self._name} "
                f"base=0x{self.base:08x} idr=0x{self.idr:08x}>")
