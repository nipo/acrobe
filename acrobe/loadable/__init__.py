from .model import Segment, Program
from . import literals  # noqa: F401
from . import bin  # noqa: F401
# Note: vendor format parsers (Altera POF/SOF/RBF, Xilinx .bit,
# Gowin .fs, Lattice .bin) have migrated to the VFS layer in
# acrobe.component.<vendor>.formats. See docs/vfs-design.md.
