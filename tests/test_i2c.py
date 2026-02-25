import asyncio
import pytest
from crobe_async.protocol.i2c import (
    Read, Write, Slave, AddressNack, DataNack,
)
from crobe_async.engine import Batcher


class MockI2cInterface(Batcher):
    """Records ops and populates Read.data with dummy data."""

    def __init__(self):
        super().__init__()
        self.ops = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Read):
                op.data = bytes(op.size)
            future.set_result(op)


class FailI2cInterface(Batcher):
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


class TestExceptions:
    def test_address_nack(self):
        exc = AddressNack(0x50)
        assert exc.addr == 0x50
        assert "0x50" in str(exc)

    def test_data_nack(self):
        exc = DataNack()
        assert isinstance(exc, Exception)


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

        writes = [op for op in iface.ops if isinstance(op, Write)]
        reads = [op for op in iface.ops if isinstance(op, Read)]
        assert len(writes) == 1
        assert len(reads) == 1
        assert writes[0].data == b"\x00"
        assert reads[0].size == 4

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
