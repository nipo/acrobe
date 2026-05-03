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

from ...db import Db, NoMatch
from ...node import Node
from . import dp as dpmod


def _idr_eq(key: int, lookup: int) -> bool:
    """IDR equality with REVISION (bits 31:28) masked off."""
    return (key & 0x0FFFFFFF) == (lookup & 0x0FFFFFFF)


class Ap(Node):
    """Access Port base. Routes register reads/writes through the
    parent DP. Concrete subclasses register against ``Ap.db`` keyed
    on their IDR signature."""

    # Register offsets common to all ADI APs.
    IDR  = 0xFC   # Identification Register
    IDR1 = 0xFD0  # ADIv6 extended identification (RES0 on ADIv5)

    db: Db = Db("AP IDR", eq_func=_idr_eq)

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
        return self._dp.post(dpmod.ApRead(ap=self.base, addr=addr))

    def reg_write(self, addr: int, data: int):
        """Post an AP register write. Returns Future -> None."""
        return self._dp.post(dpmod.ApWrite(ap=self.base, addr=addr, data=data))

    # -- Discovery --------------------------------------------------

    @classmethod
    async def discover(cls, dp: dpmod.Dp, base: int) -> "Ap | None":
        """Read IDR at the given AP base address; return an Ap of the
        most-specific registered subclass, or ``None`` if no AP is
        present (IDR = 0).

        Caller is responsible for adding the returned Ap to the DP's
        Node tree (usually via ``dp.child_add(ap)``)."""
        idr_future = dp.post(dpmod.ApRead(ap=base, addr=cls.IDR))
        try:
            idr = await idr_future
        except dpmod.DpAccessFailure as exc:
            dp.logger.protocol(
                "AP discovery at 0x%08x failed: %s", base, exc)
            return None
        if idr == 0:
            return None
        try:
            return cls.db.call(idr, dp=dp, base=base, idr=idr)
        except NoMatch:
            return cls(dp=dp, base=base, idr=idr)

    def __repr__(self):
        return (f"<{type(self).__name__} {self._name} "
                f"base=0x{self.base:08x} idr=0x{self.idr:08x}>")
