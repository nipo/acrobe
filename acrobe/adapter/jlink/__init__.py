"""SEGGER J-Link debug adapter.

Unlike ST-Link, J-Link exposes real bit-bang JTAG and SWD primitives —
the host drives the wire one bit at a time via the JTAG_IO_V3 / SWD_IO
commands. That maps directly onto acrobe's existing
:class:`JtagInterface` and the (forthcoming) `SwdInterface`, so the
slice 1-6 stack runs unchanged on top.

Phase 1 (this commit): USB transport + GET_VERSION + GET_CAPS so the
adapter shows up in ``acrobe info adapters``. Phase 2 will add JTAG
bit-bang as a ``JtagInterface`` subclass; phase 3 SWD.

References: OpenOCD's ``src/jtag/drivers/libjaylink/`` (BSD-licensed
pure C library) and ``src/jtag/drivers/jlink.c`` (GPL adapter glue).
"""

from . import adapter  # noqa: F401
