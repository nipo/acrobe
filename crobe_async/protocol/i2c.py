from __future__ import annotations

from ..engine import Batcher
from ..component import Component


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


# --- I2C Slave ---

class Slave(Batcher, Component):
    """I2C slave device at a fixed address.

    Translates read/write/write_read calls into I2C operations
    posted to the interface.
    """

    def __init__(self, interface, addr: int, name: str = None):
        if name is None:
            name = f"i2c[0x{addr:02x}]"
        Batcher.__init__(self)
        Component.__init__(self, name)
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
        for op, future in batch:
            if isinstance(op, tuple) and op[0] == "write_read":
                _, data, size = op
                write_op = Write(self.addr, data)
                read_op = Read(self.addr, size)

                write_future = self._interface.post(write_op)
                read_future = self._interface.post(read_op)
                await write_future
                result = await read_future
                future.set_result(read_op.data)
            elif isinstance(op, Read):
                iface_future = self._interface.post(op)
                await iface_future
                future.set_result(op.data)
            elif isinstance(op, Write):
                iface_future = self._interface.post(op)
                await iface_future
                future.set_result(None)

    def __repr__(self):
        return f"<Slave 0x{self.addr:02x}>"
