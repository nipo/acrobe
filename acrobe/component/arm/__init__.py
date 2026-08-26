"""ARM debug components.

Importing this package triggers the registration of ARM JTAG-DP
TAPs (and, as later slices land, AP types and CoreSight
components) in the relevant registries.
"""

from . import ap  # noqa: F401
from . import mem_ap  # noqa: F401
from . import jtag_dp  # noqa: F401
from . import sw_dp  # noqa: F401
from . import coresight  # noqa: F401
