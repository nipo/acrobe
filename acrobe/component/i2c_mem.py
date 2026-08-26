"""I2C byte-addressed memories.

An I2C memory is a slave whose transactions start with a big-endian
address prefix: writes carry the prefix followed by the payload, reads
write the prefix then restart into a sequential read that
auto-increments on the device side.

Parts wider than their address prefix fold the excess high bits into
the low bits of the slave address (``saddr_bits``): a 24lc08 has a
1-byte prefix and two banked address bits, so 0x50..0x53 select the
four 256-byte quarters of its 1 Kibit array. The device's internal
counter wraps inside a bank, so every access is split at bank
boundaries as well as at page boundaries.

:class:`I2cMem` exposes the array as an address space in the
:mod:`acrobe.protocol.memory` sense — bulk family only. There is no
register window behind an EEPROM, and a 32-bit read would just be a
blob read wearing a hat.

:class:`I2cEeprom` adds the write-cycle gate: a part burning a page
NACKs its own address until the cycle completes. The wait is expressed
as a :class:`~acrobe.protocol.i2c.WaitAck` item prefixed to the same
:class:`~acrobe.protocol.i2c.Transaction`, so the adapter polls and the
data transfer — cancelled if the poll times out — never reaches a busy
part.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..engine import Batcher, chain_future
from ..node import Addressable, Node, Writable
from ..protocol import i2c
from ..protocol import memory
from ..protocol.memory import PendingBlob


class I2cMem(memory.Interface, Batcher, Node, Writable, Addressable):
    """Byte-addressed memory behind an I2C slave address.

    Constructor parameters double as ``option_set`` keys, so a path
    like ``i2c/memory(saddr=0x50,addr_bytes=2,page_size=32)`` builds
    the same node as the equivalent Python call.
    """

    ops = memory.Interface.BULK_OPS

    DEFAULT_ADDR_BYTES = 2
    DEFAULT_PAGE_SIZE = 16
    # Sequential read auto-increments on the device, so a read is
    # bounded only by how much data one adapter transaction should
    # carry — page alignment does not apply.
    DEFAULT_READ_CHUNK = 256

    def __init__(self, bus: i2c.Interface, name: str = "i2c-mem", *,
                 saddr: int | None = None,
                 addr_bytes: int | None = None,
                 saddr_bits: int = 0,
                 size: int | None = None,
                 page_size: int | None = None,
                 read_chunk: int | None = None):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.bus = bus
        self.saddr = saddr
        self.addr_bytes = addr_bytes
        self.saddr_bits = saddr_bits
        self.total_size = size
        self.page_size = page_size
        self.read_chunk = read_chunk

    # --- Configuration ---

    def option_set(self, key, value):
        if key == "saddr":
            self.saddr = int(value, 0)
        elif key == "addr_bytes":
            self.addr_bytes = int(value, 0)
        elif key == "saddr_bits":
            self.saddr_bits = int(value, 0)
        elif key == "size":
            self.total_size = int(value, 0)
        elif key == "page_size":
            self.page_size = int(value, 0)
        elif key == "read_chunk":
            self.read_chunk = int(value, 0)

    async def start(self):
        if self.saddr is None:
            raise ValueError(
                f"{self.name}: no slave address, pass saddr=")
        if not 0 <= self.saddr <= 0x7f:
            raise ValueError(
                f"{self.name}: saddr 0x{self.saddr:x} is not a 7-bit "
                f"address")
        if self.addr_bytes is None:
            self.addr_bytes = self.DEFAULT_ADDR_BYTES
        if not 1 <= self.addr_bytes <= 4:
            raise ValueError(
                f"{self.name}: addr_bytes {self.addr_bytes} outside 1..4")
        if self.page_size is None:
            self.page_size = self.DEFAULT_PAGE_SIZE
        if self.page_size < 1:
            raise ValueError(f"{self.name}: page_size must be positive")
        if self.read_chunk is None:
            self.read_chunk = self.DEFAULT_READ_CHUNK
        if self.read_chunk < 1:
            raise ValueError(f"{self.name}: read_chunk must be positive")

        bank = self.bank_size
        if bank % self.page_size:
            raise ValueError(
                f"{self.name}: page_size {self.page_size} does not divide "
                f"the {bank}-byte address bank")

        max_size = bank << self.saddr_bits
        if self.total_size is None:
            self.total_size = max_size
        if self.total_size > max_size:
            raise ValueError(
                f"{self.name}: size {self.total_size} exceeds the "
                f"{max_size} addressable bytes")

        self.metadata.update({
            "saddr": self.saddr,
            "addr_bytes": self.addr_bytes,
            "saddr_bits": self.saddr_bits,
            "size": self.total_size,
            "page_size": self.page_size,
            "read_chunk": self.read_chunk,
        })
        self.logger.note("%d B, saddr 0x%02x, %d-byte address, page %d B",
                         self.total_size, self.saddr, self.addr_bytes,
                         self.page_size)

    @property
    def bank_size(self) -> int:
        """Bytes reachable without changing the slave address."""
        return 1 << (self.addr_bytes * 8)

    # --- Readable / Writable / Addressable contract ---

    @property
    def size(self) -> int:
        return self.total_size

    @property
    def load_address(self) -> int:
        return 0

    async def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or offset > self.total_size:
            raise ValueError(
                f"read offset {offset} outside [0, {self.total_size}]")
        return await self.mem_read(offset, min(size, self.total_size - offset))

    async def write(self, offset: int, data: bytes) -> None:
        await self.mem_write(offset, data)

    # --- Wire encoding ---

    def address_encode(self, addr: int) -> tuple[int, bytes]:
        """Split `addr` into the slave address to talk to and the
        big-endian address prefix to put on the wire."""
        low_mask = self.bank_size - 1
        bank = addr >> (self.addr_bytes * 8)
        saddr = (self.saddr & ~((1 << self.saddr_bits) - 1)) | bank
        return saddr, (addr & low_mask).to_bytes(self.addr_bytes, "big")

    def make_transaction(self, saddr: int, *items) -> i2c.Transaction:
        """Build the Transaction carrying one lowered access.

        The seam subclasses wrap: :class:`I2cEeprom` prefixes a
        write-cycle wait here without touching the decomposition.
        """
        return i2c.Transaction(items)

    # --- Address-space lowering ---

    async def flush_ops(self, batch):
        loop = asyncio.get_running_loop()
        for op, future in batch:
            try:
                if isinstance(op, memory.ReadBlob):
                    self.__lower_read(op, future, loop)
                elif isinstance(op, memory.WriteBlob):
                    self.__lower_write(op, future)
                else:
                    raise TypeError(
                        f"{type(self).__name__} can't lower "
                        f"{type(op).__name__}")
            except Exception as exc:
                if future is None:
                    raise
                future.set_exception(exc)

    def __lower_read(self, op, future, loop):
        self.__range_check(op.addr, op.size)
        pending = None
        if future is not None:
            if op.size == 0:
                future.set_result(b"")
                return
            pending = PendingBlob(future, op.size, is_read=True)
        for offset, addr, count in self.__read_chunks(op.addr, op.size):
            saddr, prefix = self.address_encode(addr)
            transfer = i2c.Transfer(saddr, data_w=prefix, size_r=count)
            tx = self.make_transaction(saddr, transfer)
            index = self.__item_index(tx, transfer)
            bus_future = self.bus.post(tx)
            if pending is None:
                bus_future.add_done_callback(self.__drop)
                continue
            sub = loop.create_future()
            chain_future(bus_future, sub, lambda items, i=index: items[i])
            pending.attach(offset, count, sub)

    def __lower_write(self, op, future):
        self.__range_check(op.addr, len(op.data))
        pending = None
        if future is not None:
            if not op.data:
                future.set_result(None)
                return
            pending = PendingBlob(future, len(op.data), is_read=False)
        for offset, addr, chunk in self.__write_chunks(op.addr, op.data):
            saddr, prefix = self.address_encode(addr)
            transfer = i2c.Transfer(saddr, data_w=prefix + chunk)
            bus_future = self.bus.post(self.make_transaction(saddr, transfer))
            if pending is None:
                bus_future.add_done_callback(self.__drop)
                continue
            pending.attach(offset, len(chunk), bus_future)

    def __range_check(self, addr: int, size: int) -> None:
        if addr < 0 or size < 0 or addr + size > self.total_size:
            raise ValueError(
                f"{self.name}: [0x{addr:x}, 0x{addr + size:x}) outside "
                f"[0, 0x{self.total_size:x})")

    def __read_chunks(self, addr: int, size: int):
        """Cover the range with chunks that neither exceed
        ``read_chunk`` nor cross a bank boundary."""
        bank = self.bank_size
        out = []
        offset = 0
        while offset < size:
            cursor = addr + offset
            count = min(self.read_chunk, size - offset,
                        bank - (cursor % bank))
            out.append((offset, cursor, count))
            offset += count
        return out

    def __write_chunks(self, addr: int, data: bytes):
        """Split the payload at page boundaries; the device's page
        buffer wraps within its page rather than carrying over."""
        out = []
        offset = 0
        while offset < len(data):
            cursor = addr + offset
            count = min(self.page_size - (cursor % self.page_size),
                        len(data) - offset)
            out.append((offset, cursor, data[offset:offset + count]))
            offset += count
        return out

    @staticmethod
    def __item_index(tx: i2c.Transaction, item) -> int:
        for index, candidate in enumerate(tx.items):
            if candidate is item:
                return index
        raise LookupError(f"{item!r} missing from {tx!r}")

    @staticmethod
    def __drop(future: asyncio.Future) -> None:
        """Consume the result of a fire-and-forget lowered access so a
        failure is not reported as an unretrieved exception."""
        if not future.cancelled():
            future.exception()

    def __repr__(self):
        if self.saddr is None:
            return f"<{type(self).__name__} {self.name} unaddressed>"
        return (f"<{type(self).__name__} {self.name} "
                f"saddr=0x{self.saddr:02x} size={self.total_size}>")


class I2cEeprom(I2cMem):
    """I2C EEPROM.

    An EEPROM stops acknowledging its address while it burns a page.
    Every access therefore opens with a ``WaitAck`` in the same
    Transaction: the adapter polls until the part answers, and the
    Transaction's cancel-on-failure semantics keep the data transfer
    from running against a device that never came back. Reads need the
    gate as much as writes — one issued right after a page write hits
    the same NACK.
    """

    DEFAULT_READY_TIMEOUT = 0.1

    def __init__(self, *args, ready_timeout: float | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready_timeout = (self.DEFAULT_READY_TIMEOUT
                              if ready_timeout is None else ready_timeout)

    def option_set(self, key, value):
        if key == "ready_timeout":
            self.ready_timeout = float(value)

    def make_transaction(self, saddr: int, *items) -> i2c.Transaction:
        return i2c.Transaction(
            (i2c.WaitAck(saddr, self.ready_timeout),) + items)


@dataclass(frozen=True)
class I2cMemPreset:
    """Constructor arguments for one known part.

    Registered against ``i2c.Interface.child_db``; the interface's
    ``child_spawn`` calls the entry with itself and the summoned name.
    """

    cls: type
    saddr: int | None = None
    addr_bytes: int | None = None
    saddr_bits: int = 0
    page_size: int | None = None
    size: int | None = None

    def __call__(self, bus: i2c.Interface, name: str) -> I2cMem:
        return self.cls(bus, name,
                        saddr=self.saddr,
                        addr_bytes=self.addr_bytes,
                        saddr_bits=self.saddr_bits,
                        page_size=self.page_size,
                        size=self.size)

    @classmethod
    def register_all(cls, presets: dict) -> None:
        for names, preset in presets.items():
            i2c.Interface.child_db.register(*names)(preset)


# Parts whose preset leaves `saddr` unset need `saddr=` in the path;
# the rest wire it at a fixed address.
PRESETS: dict[tuple[str, ...], I2cMemPreset] = {
    ("memory",): I2cMemPreset(I2cMem),
    ("eeprom",): I2cMemPreset(I2cEeprom),
    ("m24m02",): I2cMemPreset(I2cEeprom, addr_bytes=2, saddr_bits=2,
                              page_size=256),
    ("24aa64", "24fc64", "24lc64"): I2cMemPreset(
        I2cEeprom, addr_bytes=2, page_size=32, size=8 * 1024),
    ("cat24c32",): I2cMemPreset(I2cEeprom, addr_bytes=2, page_size=32,
                                size=4 * 1024),
    ("24lc128",): I2cMemPreset(I2cEeprom, addr_bytes=2, page_size=64),
    ("24lc08",): I2cMemPreset(I2cEeprom, saddr=0x50, addr_bytes=1,
                              saddr_bits=2, page_size=16),
    ("pca24s08",): I2cMemPreset(I2cEeprom, saddr=0x54, addr_bytes=1,
                                saddr_bits=2, page_size=16),
    ("pca24s08_prot",): I2cMemPreset(I2cEeprom, saddr=0x5c, addr_bytes=1,
                                     page_size=1, size=32),
}

I2cMemPreset.register_all(PRESETS)
