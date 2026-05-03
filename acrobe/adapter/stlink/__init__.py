"""ST-Link v2 / v3 debug adapter (STMicroelectronics).

Unlike FTDI-based adapters, ST-Link doesn't expose general bit-bang
JTAG. Its USB protocol is high-level: "enter JTAG / SWD mode",
"read DP register N", "read AP register N", "memory read/write
through MEM-AP". Acrobe layers these by registering chip-specific
:class:`Dp` subclasses (``StLinkJtagDp`` / ``StLinkSwDp``) that
translate batched DP/AP ops into ST-Link USB commands directly,
bypassing the bit-level :class:`JtagInterface` machinery.

Phase 1 of the port (this commit): USB plumbing + adapter
registration + GET_VERSION readback. The adapter shows up in
``acrobe info adapters`` but ``child_spawn`` for "jtag" / "swd"
isn't wired yet — that lands in phase 2.
"""

from . import adapter  # noqa: F401
