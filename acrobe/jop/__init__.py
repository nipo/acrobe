"""JTAG-over-Protocol (JoP) — Altera's TCP-framed JTAG bit-bang protocol.

Sub-modules:

* :mod:`.bytestream` — wire-level byte commands consumed by Altera's
  on-chip ``sld_hub_ctrl_core`` (the soft JTAG master that Quartus
  drives over the network).
* :mod:`.framing` — the Avalon-ST-over-TCP packet framing used between
  ``etherlink`` (or our equivalent) and Quartus' jtagd-side driver.
* :mod:`.session` — per-connection driver: decodes H2T → drives a
  :class:`JtagInterface` → encodes T2H.
* :mod:`.listener` — TCP listener implementing the 5-socket etherlink
  handshake.

The on-chip JoP byte protocol is documented in plain SystemVerilog in
``ip/.../sld_hub_ctrl_core_100/synth/sld_hub_ctrl_core.sv``; the wire
framing comes from Intel's BSD-licensed reference at
https://github.com/altera-fpga/remote-debug-for-intel-fpga.
"""
