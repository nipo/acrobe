"""VFS infrastructure and generic format parsers.

This package contains:

- The file-system root Node (`fs.py`).
- The format dispatch registries (`format_db`, `mime_db`, `ext_db`)
  for the `as(...)` reinterpretation child and auto-detection on
  `start()`.
- Generic format Nodes (ihex, bin, ELF, ZIP, tar, literals).

Vendor-specific format parsers (Altera POF, Xilinx bitstreams, etc.)
live in `acrobe.component.<vendor>` next to the related hardware
code.

See `docs/vfs-design.md`.
"""

from .fs import FsRoot, FileNode  # noqa: F401
