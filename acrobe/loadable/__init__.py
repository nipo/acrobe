from .model import Segment, Program
from . import xilinx  # noqa: F401
from . import gowin  # noqa: F401
from . import literals  # noqa: F401
from . import lattice  # noqa: F401
from . import bin  # noqa: F401
# Note: Altera POF/SOF/RBF parsing has migrated to the VFS layer
# in acrobe.component.altera.formats. See docs/vfs-design.md.
