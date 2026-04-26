from __future__ import annotations

import asyncio

from ..engine import Batcher
from ..node import Node
from ..bitstring import BitStringBase
from ..db import Db, NoMatch


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


# --- SPI Interface ---

class Interface(Batcher, Node):
    """SPI bus. Forwards Cs/Shift ops to adapter."""

    def __init__(self, adapter, name="spi"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self._adapter = adapter

    async def flush_ops(self, batch):
        futures = []
        for op, future in batch:
            futures.append((self._adapter.post(op), future))
        if futures:
            await asyncio.gather(*[f for f, _ in futures])
        for af, mf in futures:
            mf.set_result(af.result())

    def __repr__(self):
        return f"<spi.Interface {self._name}>"


# --- SPI Target ---

class Target(Batcher, Node):
    """SPI target device with CS management.

    Usage:
        result = await target.transaction(Shift(b"\\x9f", read_miso=True))
    """

    child_db = Db("SPI chip type")

    def __init__(self, interface, cs, mode: int = 0, name: str = "spi"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self._interface = interface
        self.cs = cs
        self.mode = mode

    def transaction(self, *shifts):
        """Atomic CS-held transaction. Posts Cs(assert) + shifts + Cs(deassert).
        Returns Future -> tuple of completed Shift ops (with .miso populated)."""
        return self.post(("transaction", shifts))

    async def flush_ops(self, batch):
        iface_futures = []
        result_map = []

        for idx, (op, future) in enumerate(batch):
            if isinstance(op, tuple) and op[0] == "transaction":
                _, shifts = op
                self._interface.post(Cs(self.cs, self.mode))
                shift_futures = []
                for s in shifts:
                    if self.logger.isEnabledFor(5):
                        mosi = s.mosi if isinstance(s.mosi, bytes) else bytes(s.byte_count)
                        self.logger.log(5, ">> %s", mosi.hex())
                    shift_futures.append(self._interface.post(s))
                self._interface.post(Cs(None))
                iface_futures.extend(shift_futures)
                result_map.append((idx, shifts, shift_futures))

        if iface_futures:
            await asyncio.gather(*iface_futures)

        for idx, shifts, shift_futures in result_map:
            if self.logger.isEnabledFor(5):
                for s in shifts:
                    if s.read_miso and s.miso is not None:
                        self.logger.log(5, "<< %s", s.miso.hex())
            batch[idx][1].set_result(tuple(shifts))

    async def child_spawn(self, name):
        return await self.child_db.acall(name, self)

    def __repr__(self):
        return f"<Target cs={self.cs} mode={self.mode}>"
