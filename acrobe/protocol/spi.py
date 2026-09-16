from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..engine import Batcher
from ..freq_capper import FreqCapper
from ..node import Node
from ..bitstring import BitString, BitStringBase
from ..db import Db, NoMatch


# SPI Interface Operations
#
# All op classes are immutable. The future returned by Batcher.post()
# resolves to the operation's natural result value:
#   - Shift(read_miso=True)  → BitString as long as ``mosi``
#   - Shift(read_miso=False) → None
#   - Cs                     → None
#   - Transaction            → tuple of the above, one per shift
# A shift is full duplex: MOSI and MISO carry the same number of
# bits, so a read-only shift is expressed by handing it as many zero
# bits as it should capture.


@dataclass(frozen=True, slots=True)
class Cs:
    """Chip select control.

    ``value`` names the select line to assert, None releases it.
    ``mode`` is the SPI mode (CPOL << 1 | CPHA) the shifts under this
    assertion run in.
    """

    value: int | None
    mode: int = 0

    def __repr__(self):
        if self.value is not None:
            return f"<Cs {self.value} mode={self.mode}>"
        return "<Cs None>"


@dataclass(frozen=True, slots=True)
class Shift:
    """SPI data shift operation.

    ``mosi`` accepts a BitString, bytes, or an integer byte count
    standing for that many zero bytes; it is normalised to a
    BitString.
    """

    mosi: BitString
    read_miso: bool = True

    def __post_init__(self):
        mosi = self.mosi
        if isinstance(mosi, BitString):
            return
        if isinstance(mosi, int):
            if mosi < 0:
                raise ValueError(f"Shift byte count must be >= 0, got {mosi}")
            mosi = BitString(0, mosi * 8)
        elif isinstance(mosi, BitStringBase):
            mosi = BitString(bytes(mosi), len(mosi))
        else:
            blob = bytes(mosi)
            mosi = BitString(blob, len(blob) * 8)
        object.__setattr__(self, "mosi", mosi)

    @property
    def byte_count(self) -> int:
        return (len(self.mosi) + 7) // 8

    def __repr__(self):
        bits = len(self.mosi)
        size = f"{bits // 8}B" if bits % 8 == 0 else f"{bits}b"
        return f"<Shift {size} read={self.read_miso}>"


@dataclass(frozen=True, slots=True)
class Transaction:
    """Sequence of shifts held under one chip-select assertion."""

    shifts: tuple

    def __post_init__(self):
        if not isinstance(self.shifts, tuple):
            object.__setattr__(self, "shifts", tuple(self.shifts))
        if not self.shifts:
            raise ValueError("Transaction must have at least one shift")
        for s in self.shifts:
            if not isinstance(s, Shift):
                raise TypeError(f"Invalid Transaction item: {s!r}")

    def __repr__(self):
        return f"Transaction({', '.join(repr(s) for s in self.shifts)})"


# --- SPI Interface ---

class Interface(Batcher, FreqCapper, Node):
    """SPI bus. Forwards Cs/Shift ops to adapter.

    Mixes in :class:`FreqCapper` so ``fmax=`` path options work on
    any concrete SPI interface; subclasses with clock control
    override :meth:`FreqCapper.freq_update`. Subclasses whose clock
    hardware only comes up in start() call :meth:`freq_reapply`
    there to push caps recorded before that point.
    """

    def __init__(self, adapter, name="spi"):
        Batcher.__init__(self)
        FreqCapper.__init__(self)
        Node.__init__(self, name)
        self.__adapter = adapter

    async def flush_ops(self, batch):
        futures = []
        for op, future in batch:
            futures.append((self.__adapter.post(op), future))
        if futures:
            await asyncio.gather(*[f for f, _ in futures])
        for af, mf in futures:
            mf.set_result(af.result())

    def __repr__(self):
        return f"<spi.Interface {self.name}>"


# --- SPI Target ---

class Target(Batcher, Node):
    """SPI target device with CS management.

    Usage:
        miso, = await target.transaction(Shift(b"\\x9f", read_miso=True))
    """

    child_db = Db("SPI chip type")

    def __init__(self, interface, cs, mode: int = 0, name: str = "spi"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__interface = interface
        self.cs = cs
        self.mode = mode

    def transaction(self, *shifts):
        """Atomic CS-held transaction.

        Future -> tuple of per-shift results (BitString for a reading
        shift, None otherwise).
        """
        return self.post(Transaction(shifts))

    async def flush_ops(self, batch):
        iface_futures = []
        result_map = []
        verbose = self.logger.isEnabledFor(5)

        for idx, (op, future) in enumerate(batch):
            if not isinstance(op, Transaction):
                future.set_exception(TypeError(
                    f"spi.Target cannot lower {type(op).__name__}"))
                continue
            self.__interface.post(Cs(self.cs, self.mode))
            shift_futures = []
            for s in op.shifts:
                if verbose:
                    self.logger.log(5, ">> %s", bytes(s.mosi).hex())
                shift_futures.append(self.__interface.post(s))
            self.__interface.post(Cs(None))
            iface_futures.extend(shift_futures)
            result_map.append((idx, shift_futures))

        if iface_futures:
            await asyncio.gather(*iface_futures)

        for idx, shift_futures in result_map:
            results = tuple(f.result() for f in shift_futures)
            if verbose:
                for miso in results:
                    if miso is not None:
                        self.logger.log(5, "<< %s", bytes(miso).hex())
            batch[idx][1].set_result(results)

    async def child_spawn(self, name):
        return await self.child_db.acall(name, self)

    def __repr__(self):
        return f"<Target cs={self.cs} mode={self.mode}>"
