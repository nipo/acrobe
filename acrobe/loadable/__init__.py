from .model import Segment, Program
# Note: All format parsers have migrated to the VFS layer:
# - generic (bin, ihex, elf, literals, zip, tar): acrobe.vfs.*
# - vendor (altera, xilinx, gowin, lattice): acrobe.component.<vendor>.formats
# Program / Segment are kept until Step 12 (dissolution).
# See docs/vfs-design.md.
