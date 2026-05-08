import asyncio
import pytest
from acrobe.protocol.i2c import (
    Read, Write, WriteRead, Slave, Interface, AddressNack, DataNack,
)
from acrobe.engine import Batcher
from acrobe.node import Node


class _NodeBatcher(Batcher, Node):
    def __init__(self, name: str = None):
        Batcher.__init__(self)
        Node.__init__(self, name or self.__class__.__name__.lower())


class MockI2cAdapter(_NodeBatcher):
    """Records ops and populates Read/WriteRead results with dummy data."""

    def __init__(self):
        super().__init__()
        self.ops = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Read):
                op.data = bytes(op.size)
            elif isinstance(op, WriteRead):
                op.result = bytes(op.size)
            future.set_result(op)


class MockI2cInterface(_NodeBatcher):
    """Interface-level mock (accepts Read/Write/WriteRead ops)."""

    def __init__(self):
        super().__init__()
        self.ops = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Read):
                op.data = bytes(op.size)
            elif isinstance(op, WriteRead):
                op.result = bytes(op.size)
            future.set_result(op)


class FailI2cInterface(_NodeBatcher):
    """Raises AddressNack for all operations."""

    async def flush_ops(self, batch):
        for op, future in batch:
            future.set_exception(AddressNack(op.addr))


class TestReadOp:
    def test_basic(self):
        op = Read(0x50, 4)
        assert op.addr == 0x50
        assert op.size == 4
        assert op.data is None

    def test_repr(self):
        op = Read(0x50, 4)
        assert "0x50" in repr(op)
        assert "4B" in repr(op)


class TestWriteOp:
    def test_basic(self):
        op = Write(0x50, b"\x01\x02")
        assert op.addr == 0x50
        assert op.data == b"\x01\x02"

    def test_data_is_bytes(self):
        op = Write(0x50, bytearray(b"\x01"))
        assert isinstance(op.data, bytes)

    def test_repr(self):
        op = Write(0x50, b"\x01\x02")
        assert "0x50" in repr(op)


class TestWriteReadOp:
    def test_basic(self):
        op = WriteRead(0x50, b"\x00", 4)
        assert op.addr == 0x50
        assert op.data == b"\x00"
        assert op.size == 4
        assert op.result is None

    def test_data_is_bytes(self):
        op = WriteRead(0x50, bytearray(b"\x01"), 2)
        assert isinstance(op.data, bytes)

    def test_repr(self):
        op = WriteRead(0x50, b"\x00\x01", 4)
        assert "0x50" in repr(op)
        assert "w=2B" in repr(op)
        assert "r=4B" in repr(op)


class TestExceptions:
    def test_address_nack(self):
        exc = AddressNack(0x50)
        assert exc.addr == 0x50
        assert "0x50" in str(exc)

    def test_data_nack(self):
        exc = DataNack()
        assert isinstance(exc, Exception)


class TestInterface:
    @pytest.mark.asyncio
    async def test_passthrough_read(self):
        adapter = MockI2cAdapter()
        iface = Interface(adapter)
        op = Read(0x50, 4)
        result = await iface.post(op)
        assert result is op
        assert op.data == bytes(4)
        assert len(adapter.ops) == 1

    @pytest.mark.asyncio
    async def test_passthrough_write(self):
        adapter = MockI2cAdapter()
        iface = Interface(adapter)
        op = Write(0x50, b"\x01\x02")
        result = await iface.post(op)
        assert result is op
        assert len(adapter.ops) == 1

    @pytest.mark.asyncio
    async def test_passthrough_write_read(self):
        adapter = MockI2cAdapter()
        iface = Interface(adapter)
        op = WriteRead(0x50, b"\x00", 4)
        result = await iface.post(op)
        assert result is op
        assert op.result == bytes(4)

    @pytest.mark.asyncio
    async def test_batching(self):
        adapter = MockI2cAdapter()
        iface = Interface(adapter)
        r = Read(0x50, 2)
        w = Write(0x50, b"\x01")
        f1 = iface.post(r)
        f2 = iface.post(w)
        await asyncio.gather(f1, f2)
        assert len(adapter.ops) == 2

    def test_repr(self):
        adapter = MockI2cAdapter()
        iface = Interface(adapter, name="bus0")
        assert "Interface" in repr(iface)


class TestSlave:
    @pytest.mark.asyncio
    async def test_read(self):
        iface = MockI2cInterface()
        slave = Slave(iface, addr=0x50)
        result = await slave.read(4)
        assert result == bytes(4)

        reads = [op for op in iface.ops if isinstance(op, Read)]
        assert len(reads) == 1
        assert reads[0].addr == 0x50
        assert reads[0].size == 4

    @pytest.mark.asyncio
    async def test_write(self):
        iface = MockI2cInterface()
        slave = Slave(iface, addr=0x50)
        result = await slave.write(b"\x01\x02")
        assert result is None

        writes = [op for op in iface.ops if isinstance(op, Write)]
        assert len(writes) == 1
        assert writes[0].addr == 0x50
        assert writes[0].data == b"\x01\x02"

    @pytest.mark.asyncio
    async def test_write_read(self):
        iface = MockI2cInterface()
        slave = Slave(iface, addr=0x50)
        result = await slave.write_read(b"\x00", 4)
        assert result == bytes(4)

        wr_ops = [op for op in iface.ops if isinstance(op, WriteRead)]
        assert len(wr_ops) == 1
        assert wr_ops[0].addr == 0x50
        assert wr_ops[0].data == b"\x00"
        assert wr_ops[0].size == 4

    @pytest.mark.asyncio
    async def test_batching(self):
        """Multiple ops posted before await should batch together."""
        iface = MockI2cInterface()
        slave = Slave(iface, addr=0x50)
        f1 = slave.read(2)
        f2 = slave.write(b"\x01")
        f3 = slave.write_read(b"\x00", 4)
        r1, r2, r3 = await asyncio.gather(f1, f2, f3)
        assert r1 == bytes(2)
        assert r2 is None
        assert r3 == bytes(4)
        # All 3 ops should have been posted to the interface in one batch
        assert len(iface.ops) == 3

    @pytest.mark.asyncio
    async def test_default_name(self):
        iface = MockI2cInterface()
        slave = Slave(iface, addr=0x50)
        assert "0x50" in slave.name

    @pytest.mark.asyncio
    async def test_custom_name(self):
        iface = MockI2cInterface()
        slave = Slave(iface, addr=0x50, name="eeprom")
        assert slave.name == "eeprom"

    @pytest.mark.asyncio
    async def test_error_propagates(self):
        slave = Slave(FailI2cInterface(), addr=0x50)
        with pytest.raises(AddressNack):
            await slave.read(4)

    def test_repr(self):
        iface = MockI2cInterface()
        slave = Slave(iface, addr=0x50)
        assert "0x50" in repr(slave)
