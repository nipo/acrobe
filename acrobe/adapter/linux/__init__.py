"""Adapters for buses the Linux kernel already drives.

An embedded host is often the SPI/I²C master itself: the SoC's
controllers surface as ``/dev/spidevX.Y`` and ``/dev/i2c-N`` and need
no external probe. These modules expose them as ordinary acrobe
adapters, so everything above layer 1 — SFDP flash discovery, the
I²C memory presets, targets, the CLI — works unchanged.
"""
