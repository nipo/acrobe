"""Xilinx Virtual Cable (XVC) — TCP-framed JTAG bit-bang protocol.

Sub-modules:

* :mod:`.wire` — protocol constants, command parsers, on-the-wire
  string formats.
* :mod:`.session` — per-connection driver: decodes ``shift:`` bursts,
  walks them through :class:`JtagTmsWalker`, returns TDO.
* :mod:`.listener` — single-client TCP listener.

XVC is much smaller than JoP: three commands (``getinfo:``,
``settck:``, ``shift:``) over a plain TCP stream. There is no
synchronisation in the protocol, so we only ever serve one client at
a time — additional connections are closed immediately.

Reference: Xilinx UG1037, "Virtual Cable Protocol".
"""
