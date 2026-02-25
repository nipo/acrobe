from __future__ import annotations

from ..engine import Batcher
from ..component import Component
from ..bitstring import BitStringBase


# --- SPI Operations ---

class Cs:
    """Chip select control."""

    def __init__(self, value, mode: int = 0):
        self.value = value
        self.mode = mode

    def __repr__(self):
        if self.value is not None:
            return f"<Cs {self.value} mode={self.mode}>"
        return "<Cs None>"


class Shift:
    """SPI data shift operation."""

    def __init__(self, mosi, read_miso: bool = True):
        if isinstance(mosi, int):
            # Byte count for read-only
            self.mosi = bytes(mosi)
            self.byte_count = mosi
        elif isinstance(mosi, (bytes, bytearray)):
            self.mosi = bytes(mosi)
            self.byte_count = len(self.mosi)
        elif isinstance(mosi, BitStringBase):
            self.mosi = mosi
            self.byte_count = (len(mosi) + 7) // 8
        else:
            self.mosi = bytes(mosi)
            self.byte_count = len(self.mosi)
        self.read_miso = read_miso
        self.miso = None

    def __repr__(self):
        return f"<Shift {self.byte_count}B read={self.read_miso}>"


# --- SPI Target ---

class Target(Batcher, Component):
    """SPI target device with CS management.

    Usage:
        result = await target.transaction(b"\\x9f", read_miso=True)
    """

    def __init__(self, interface, cs, mode: int = 0, name: str = "spi"):
        Batcher.__init__(self)
        Component.__init__(self, name)
        self._interface = interface
        self.cs = cs
        self.mode = mode

    def shift(self, mosi, read_miso: bool = True):
        """Post a shift operation. Returns Future -> Shift op."""
        return self.post(Shift(mosi, read_miso))

    def transaction(self, mosi, read_miso: bool = True):
        """Post an atomic CS-assert, shift, CS-deassert. Returns Future -> miso bytes."""
        return self.post(("transaction", mosi, read_miso))

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, tuple) and op[0] == "transaction":
                _, mosi, read_miso = op
                shift = Shift(mosi, read_miso)
                cs_on = Cs(self.cs, self.mode)
                cs_off = Cs(None)

                await self._interface.post(cs_on)
                shift_future = self._interface.post(shift)
                await shift_future
                await self._interface.post(cs_off)

                future.set_result(shift.miso)
            elif isinstance(op, Shift):
                shift_future = self._interface.post(op)
                result = await shift_future
                future.set_result(result)

    def __repr__(self):
        return f"<Target cs={self.cs} mode={self.mode}>"
