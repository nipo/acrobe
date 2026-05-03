"""CoreSight components (class-0x1 ROM tables, class-0x9 components).

Importing this package triggers the registration of CoreSight
components against the relevant Dbs on `MemoryMappedComponent` and
`CoresightComponent`.
"""

from . import model  # noqa: F401
from . import power_gate  # noqa: F401
from . import rom_table  # noqa: F401
