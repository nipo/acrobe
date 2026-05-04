"""CoreSight components (class-0x1 ROM tables, class-0x9 components).

Importing this package triggers the registration of CoreSight
components against the relevant Dbs on `MemoryMappedComponent` and
`CoresightComponent`.
"""

# Core machinery.
from . import model       # noqa: F401
from . import power_gate  # noqa: F401
from . import rom_table   # noqa: F401

# Concrete component drivers (registrations fire at import time).
from . import bus_trace      # noqa: F401
from . import coproc_trace   # noqa: F401
from . import cti            # noqa: F401
from . import dbg            # noqa: F401
from . import dwt            # noqa: F401
from . import etb            # noqa: F401
from . import etm            # noqa: F401
from . import fpb            # noqa: F401
from . import funnel         # noqa: F401
from . import itm            # noqa: F401
from . import pmu            # noqa: F401
from . import router         # noqa: F401
from . import scs            # noqa: F401
from . import stm            # noqa: F401
from . import tpiu           # noqa: F401
from . import tsgen          # noqa: F401
