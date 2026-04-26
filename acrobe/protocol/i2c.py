from __future__ import annotations

import asyncio

from ..engine import Batcher
from ..node import Node


class AddressNack(Exception):
    """Raised when I2C slave does not acknowledge its address."""

    def __init__(self, addr: int):
        self.addr = addr
        super().__init__(f"I2C address NACK at 0x{addr:02x}")


class DataNack(Exception):
    """Raised when I2C slave NACKs during data transfer."""
    pass


# --- I2C Operations ---

class Read:
    """I2C read operation."""

    def __init__(self, addr: int, size: int):
        self.addr = addr
        self.size = size
        self.data = None

    def __repr__(self):
        return f"<Read 0x{self.addr:02x} {self.size}B>"


class Write:
    """I2C write operation."""

    def __init__(self, addr: int, data: bytes):
        self.addr = addr
        self.data = bytes(data)

    def __repr__(self):
        return f"<Write 0x{self.addr:02x} {len(self.data)}B>"


class WriteRead:
    """I2C write-then-read with repeated start."""

    def __init__(self, addr: int, data: bytes, size: int):
        self.addr = addr
        self.data = bytes(data)
        self.size = size
        self.result = None  # populated by adapter

    def __repr__(self):
        return f"<WriteRead 0x{self.addr:02x} w={len(self.data)}B r={self.size}B>"


# --- I2C Interface ---

class Interface(Batcher, Node):
    """I2C bus. Forwards Read/Write/WriteRead to adapter."""

    def __init__(self, adapter, name="i2c"):
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
        return f"<i2c.Interface {self._name}>"


# --- I2C Slave ---

class Slave(Batcher, Node):
    """I2C slave device at a fixed address.

    Translates read/write/write_read calls into I2C operations
    posted to the interface.
    """

    def __init__(self, interface, addr: int, name: str = None):
        if name is None:
            name = f"i2c[0x{addr:02x}]"
        Batcher.__init__(self)
        Node.__init__(self, name)
        self._interface = interface
        self.addr = addr

    def read(self, size: int):
        """Post a read. Returns Future -> bytes."""
        return self.post(Read(self.addr, size))

    def write(self, data: bytes):
        """Post a write. Returns Future -> None."""
        return self.post(Write(self.addr, data))

    def write_read(self, data: bytes, size: int):
        """Post a write-then-read (repeated start). Returns Future -> bytes."""
        return self.post(("write_read", data, size))

    async def flush_ops(self, batch):
        iface_futures = []
        read_info = []

        for idx, (op, future) in enumerate(batch):
            if isinstance(op, tuple) and op[0] == "write_read":
                _, data, size = op
                wr_op = WriteRead(self.addr, data, size)
                f = self._interface.post(wr_op)
                iface_futures.append(f)
                read_info.append((idx, wr_op))
            elif isinstance(op, Read):
                f = self._interface.post(op)
                iface_futures.append(f)
                read_info.append((idx, op))
            elif isinstance(op, Write):
                f = self._interface.post(op)
                iface_futures.append(f)
                read_info.append((idx, None))

        if iface_futures:
            await asyncio.gather(*iface_futures)

        for idx, read_op in read_info:
            op, future = batch[idx]
            if isinstance(op, tuple):
                future.set_result(read_op.result)
            elif isinstance(op, Read):
                future.set_result(op.data)
            else:
                future.set_result(None)

    def __repr__(self):
        return f"<Slave 0x{self.addr:02x}>"
