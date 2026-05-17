from __future__ import annotations

from dataclasses import dataclass

from ..engine import Batcher
from ..node import Node


# I2C operations, after the Transfer / WaitAck / Transaction model:
#
# * Transfer is the only wire-level primitive: one START, addressed
#   slave, optional write payload, optional restart-then-read, STOP.
#   This matches what real adapters (FTDI, Proby, kernel i2c-dev, …)
#   actually expose; multi-restart sequences past write-then-read are
#   not supported by typical hardware and are deliberately not
#   modeled.
# * WaitAck is a probe loop: START + addr + STOP repeatedly until the
#   slave acknowledges or the timeout elapses. Useful only as a gate
#   for a following Transfer (EEPROM write-cycle wait, ADC busy
#   poll); it offloads polling latency to the adapter when supported.
# * Transaction groups a sequence of Transfer / WaitAck items with
#   cancel-on-failure semantics: an item that raises aborts every
#   later item in the same Transaction.
#
# A naked Transfer or WaitAck posted to the Interface is wrapped into
# a 1-item Transaction before reaching the adapter; the adapter's
# contract is therefore uniform — it consumes Transactions and
# resolves the future to ``tuple[bytes | None, ...]`` aligned with
# ``Transaction.items`` (``bytes`` for read Transfers, ``None`` for
# write-only Transfers and WaitAcks), or raises on failure.
#
# Per acrobe convention, op classes are frozen dataclasses; the
# Future returned by Batcher.post() resolves to the natural result:
#   - Transfer with size_r > 0  → bytes
#   - Transfer write-only       → None
#   - WaitAck                   → None
#   - Transaction               → tuple of the above per item


# ---- Exceptions ----

class AddressNack(Exception):
    """Slave did not acknowledge its address."""

    def __init__(self, addr: int):
        self.addr = addr
        super().__init__(f"I2C address NACK at 0x{addr:02x}")


class DataNack(Exception):
    """Slave NACKed during data transfer."""

    def __init__(self, addr: int):
        self.addr = addr
        super().__init__(f"I2C data NACK at 0x{addr:02x}")


class WaitAckTimeout(Exception):
    """WaitAck did not see an address ACK within the timeout."""

    def __init__(self, addr: int, timeout_s: float):
        self.addr = addr
        self.timeout_s = timeout_s
        super().__init__(
            f"I2C WaitAck timeout at 0x{addr:02x} after {timeout_s}s")


# ---- Operations ----

@dataclass(frozen=True, slots=True)
class Transfer:
    """One atomic START..STOP bus transaction.

    ``data_w`` empty → read-only.  ``size_r`` zero → write-only.
    Both non-zero → write-then-read with one repeated start between.
    """

    addr: int
    data_w: bytes = b""
    size_r: int = 0

    def __post_init__(self):
        if not isinstance(self.data_w, bytes):
            object.__setattr__(self, "data_w", bytes(self.data_w))
        if not self.data_w and self.size_r == 0:
            raise ValueError("Transfer must read or write at least one byte")
        if self.size_r < 0:
            raise ValueError("Transfer size_r must be non-negative")

    def __repr__(self):
        if self.data_w and self.size_r:
            return (f"Transfer(0x{self.addr:02x}, "
                    f"w={self.data_w.hex()}, r={self.size_r}B)")
        if self.size_r:
            return f"Transfer(0x{self.addr:02x}, r={self.size_r}B)"
        return f"Transfer(0x{self.addr:02x}, w={self.data_w.hex()})"


@dataclass(frozen=True, slots=True)
class WaitAck:
    """Probe a slave's address until it ACKs or the timeout elapses."""

    addr: int
    timeout_s: float
    interval_s: float | None = None

    def __post_init__(self):
        if self.timeout_s <= 0:
            raise ValueError("WaitAck timeout_s must be positive")
        if self.interval_s is not None and self.interval_s <= 0:
            raise ValueError("WaitAck interval_s must be positive when set")

    def __repr__(self):
        return f"WaitAck(0x{self.addr:02x}, t<={self.timeout_s}s)"


@dataclass(frozen=True, slots=True)
class Transaction:
    """Sequence of Transfer / WaitAck items.

    Failure of one item aborts every later item in the Transaction;
    the future resolves with the failing item's exception.
    """

    items: tuple

    def __post_init__(self):
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))
        if not self.items:
            raise ValueError("Transaction must have at least one item")
        for it in self.items:
            if not isinstance(it, (Transfer, WaitAck)):
                raise TypeError(f"Invalid Transaction item: {it!r}")

    def __repr__(self):
        return f"Transaction({', '.join(repr(i) for i in self.items)})"


# ---- Interface ----

class Interface(Batcher, Node):
    """I2C bus.

    Accepts Transfer, WaitAck, or Transaction ops.  Naked items are
    wrapped into a 1-item Transaction before forwarding so the
    adapter only ever sees Transactions.
    """

    def __init__(self, adapter, name: str = "i2c"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__adapter = adapter

    @staticmethod
    def __normalize(op):
        """Return (Transaction, single).  ``single`` means the caller
        passed a naked item and expects an unwrapped result."""
        if isinstance(op, Transaction):
            return op, False
        if isinstance(op, (Transfer, WaitAck)):
            return Transaction((op,)), True
        raise TypeError(f"Unsupported I2C op: {op!r}")

    async def flush_ops(self, batch):
        forwarded = []
        for op, future in batch:
            tx, single = self.__normalize(op)
            forwarded.append((self.__adapter.post(tx), future, single))

        for af, mf, single in forwarded:
            try:
                result = await af
            except Exception as exc:
                mf.set_exception(exc)
                continue
            mf.set_result(result[0] if single else result)

    def __repr__(self):
        return f"<i2c.Interface {self._name}>"


# ---- Slave ----

class Slave(Node):
    """I2C slave at a fixed address.  Thin facade over Interface."""

    def __init__(self, interface: Interface, addr: int, name: str = None):
        if name is None:
            name = f"i2c[0x{addr:02x}]"
        Node.__init__(self, name)
        self.__interface = interface
        self.addr = addr

    def read(self, size: int):
        """Future → bytes."""
        return self.__interface.post(Transfer(self.addr, size_r=size))

    def write(self, data):
        """Future → None."""
        return self.__interface.post(Transfer(self.addr, data_w=bytes(data)))

    def write_read(self, data, size: int):
        """Future → bytes."""
        return self.__interface.post(
            Transfer(self.addr, data_w=bytes(data), size_r=size))

    def wait_ready(self, timeout_s: float, interval_s: float | None = None):
        """Future → None.  Raises WaitAckTimeout on timeout."""
        return self.__interface.post(
            WaitAck(self.addr, timeout_s, interval_s))

    def transaction(self, *items):
        """Future → tuple of per-item natural results."""
        return self.__interface.post(Transaction(items))

    def post(self, op):
        """Forward a pre-built op (Transaction, Transfer, WaitAck) directly."""
        return self.__interface.post(op)

    def __repr__(self):
        return f"<Slave 0x{self.addr:02x}>"
