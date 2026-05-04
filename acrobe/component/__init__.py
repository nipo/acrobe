"""Hardware components and vendor-specific format parsers.

Importing this package triggers the registration of vendor
hardware Nodes and their format parsers in the relevant
registries. The Node base class itself lives at `acrobe.node`.
"""

from . import altera  # noqa: F401
from . import xilinx  # noqa: F401
from . import gowin  # noqa: F401
from . import lattice  # noqa: F401
from . import spi_flash  # noqa: F401
from . import arm  # noqa: F401
from . import renesas  # noqa: F401
