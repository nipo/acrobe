import asyncio
import pytest

from acrobe.protocol.i2c import (
    Transfer, WaitAck, Transaction,
    Interface, Slave,
    AddressNack, DataNack, WaitAckTimeout,
)
from acrobe.engine import Batcher
from acrobe.node import Node


class _NodeBatcher(Batcher, Node):
    def __init__(self, name: str = None):
        Batcher.__init__(self)
        Node.__init__(self, name or self.__class__.__name__.lower())


class MockAdapter(_NodeBatcher):
    """Adapter contract: accepts Transactions only.

    The default ``responder`` returns the natural per-item result for
    each item — zero-filled bytes for read Transfers, None otherwise.
    Tests can install a different responder to simulate failures or
    real data.  ``transactions`` records every Transaction received.
    """

    def __init__(self):
        super().__init__()
        self.transactions = []
        self.responder = self._default_responder

    @staticmethod
    def _default_responder(items):
        return tuple(
            bytes(it.size_r) if isinstance(it, Transfer) and it.size_r else None
            for it in items)

    async def flush_ops(self, batch):
        for op, future in batch:
            self.transactions.append(op)
            assert isinstance(op, Transaction), \
                f"Adapter contract: Transaction only, got {op!r}"
            try:
                future.set_result(self.responder(op.items))
            except Exception as exc:
                future.set_exception(exc)


class TestTransfer:
    def test_write_only(self):
        op = Transfer(0x50, data_w=b"\x01\x02")
        assert op.addr == 0x50
        assert op.data_w == b"\x01\x02"
        assert op.size_r == 0

    def test_read_only(self):
        op = Transfer(0x50, size_r=4)
        assert op.data_w == b""
        assert op.size_r == 4

    def test_write_then_read(self):
        op = Transfer(0x50, data_w=b"\x00", size_r=4)
        assert op.data_w == b"\x00"
        assert op.size_r == 4

    def test_data_coerced_to_bytes(self):
        op = Transfer(0x50, data_w=bytearray(b"\x01"))
        assert isinstance(op.data_w, bytes)

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            Transfer(0x50)

    def test_negative_size_rejected(self):
        with pytest.raises(ValueError):
            Transfer(0x50, size_r=-1)

    def test_frozen(self):
        op = Transfer(0x50, size_r=4)
        with pytest.raises((AttributeError, TypeError)):
            op.addr = 0x51  # type: ignore[misc]

    def test_repr(self):
        assert "0x50" in repr(Transfer(0x50, size_r=4))
        assert "0x50" in repr(Transfer(0x50, data_w=b"\x01"))
        assert "w=" in repr(Transfer(0x50, data_w=b"\x00", size_r=4))
        assert "r=" in repr(Transfer(0x50, data_w=b"\x00", size_r=4))


class TestWaitAck:
    def test_basic(self):
        op = WaitAck(0x50, timeout_s=0.05)
        assert op.addr == 0x50
        assert op.timeout_s == 0.05
        assert op.interval_s is None

    def test_negative_timeout_rejected(self):
        with pytest.raises(ValueError):
            WaitAck(0x50, timeout_s=0)

    def test_negative_interval_rejected(self):
        with pytest.raises(ValueError):
            WaitAck(0x50, timeout_s=0.1, interval_s=0)

    def test_repr(self):
        assert "0x50" in repr(WaitAck(0x50, timeout_s=0.05))


class TestTransaction:
    def test_basic(self):
        tx = Transaction((Transfer(0x50, size_r=2),))
        assert len(tx.items) == 1

    def test_iterable_normalized_to_tuple(self):
        tx = Transaction([Transfer(0x50, size_r=2), WaitAck(0x50, 0.01)])
        assert isinstance(tx.items, tuple)
        assert len(tx.items) == 2

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            Transaction(())

    def test_invalid_item_rejected(self):
        with pytest.raises(TypeError):
            Transaction(("not-an-op",))


class TestExceptions:
    def test_address_nack(self):
        exc = AddressNack(0x50)
        assert exc.addr == 0x50
        assert "0x50" in str(exc)

    def test_data_nack(self):
        exc = DataNack(0x50)
        assert exc.addr == 0x50
        assert "0x50" in str(exc)

    def test_wait_ack_timeout(self):
        exc = WaitAckTimeout(0x50, 0.05)
        assert exc.addr == 0x50
        assert exc.timeout_s == 0.05


class TestInterface:
    @pytest.mark.asyncio
    async def test_naked_transfer_read_unwraps(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        result = await iface.post(Transfer(0x50, size_r=4))
        assert result == bytes(4)
        # Adapter saw exactly one Transaction wrapping the Transfer.
        assert len(adapter.transactions) == 1
        assert isinstance(adapter.transactions[0], Transaction)
        assert len(adapter.transactions[0].items) == 1

    @pytest.mark.asyncio
    async def test_naked_transfer_write_returns_none(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        result = await iface.post(Transfer(0x50, data_w=b"\x01\x02"))
        assert result is None

    @pytest.mark.asyncio
    async def test_naked_transfer_write_read_unwraps(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        result = await iface.post(Transfer(0x50, data_w=b"\x00", size_r=4))
        assert result == bytes(4)

    @pytest.mark.asyncio
    async def test_naked_wait_ack_returns_none(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        result = await iface.post(WaitAck(0x50, timeout_s=0.05))
        assert result is None

    @pytest.mark.asyncio
    async def test_transaction_returns_tuple(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        tx = Transaction((
            Transfer(0x50, data_w=b"\x00"),
            WaitAck(0x50, timeout_s=0.05),
            Transfer(0x50, size_r=4),
        ))
        result = await iface.post(tx)
        assert result == (None, None, bytes(4))

    @pytest.mark.asyncio
    async def test_batching_across_slaves(self):
        """Two posts to different slaves coalesce into one adapter batch."""
        adapter = MockAdapter()
        iface = Interface(adapter)
        f1 = iface.post(Transfer(0x50, size_r=2))
        f2 = iface.post(Transfer(0x60, data_w=b"\x01"))
        await asyncio.gather(f1, f2)
        assert len(adapter.transactions) == 2
        assert adapter.transactions[0].items[0].addr == 0x50
        assert adapter.transactions[1].items[0].addr == 0x60

    @pytest.mark.asyncio
    async def test_failure_propagates(self):
        adapter = MockAdapter()

        def fail(items):
            raise AddressNack(items[0].addr)
        adapter.responder = fail

        iface = Interface(adapter)
        with pytest.raises(AddressNack):
            await iface.post(Transfer(0x50, size_r=4))

    @pytest.mark.asyncio
    async def test_unsupported_op_rejected(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        with pytest.raises(TypeError):
            await iface.post("garbage")

    def test_repr(self):
        adapter = MockAdapter()
        iface = Interface(adapter, name="bus0")
        assert "Interface" in repr(iface)


class TestSlave:
    @pytest.mark.asyncio
    async def test_read(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        slave = Slave(iface, addr=0x50)
        result = await slave.read(4)
        assert result == bytes(4)
        assert adapter.transactions[0].items[0] == Transfer(0x50, size_r=4)

    @pytest.mark.asyncio
    async def test_write(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        slave = Slave(iface, addr=0x50)
        result = await slave.write(b"\x01\x02")
        assert result is None
        assert adapter.transactions[0].items[0] == Transfer(
            0x50, data_w=b"\x01\x02")

    @pytest.mark.asyncio
    async def test_write_read(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        slave = Slave(iface, addr=0x50)
        result = await slave.write_read(b"\x00", 4)
        assert result == bytes(4)
        assert adapter.transactions[0].items[0] == Transfer(
            0x50, data_w=b"\x00", size_r=4)

    @pytest.mark.asyncio
    async def test_wait_ready_success(self):
        adapter = MockAdapter()
        iface = Interface(adapter)
        slave = Slave(iface, addr=0x50)
        result = await slave.wait_ready(timeout_s=0.05)
        assert result is None
        assert isinstance(adapter.transactions[0].items[0], WaitAck)

    @pytest.mark.asyncio
    async def test_wait_ready_timeout(self):
        adapter = MockAdapter()

        def time_out(items):
            it = items[0]
            raise WaitAckTimeout(it.addr, it.timeout_s)
        adapter.responder = time_out

        iface = Interface(adapter)
        slave = Slave(iface, addr=0x50)
        with pytest.raises(WaitAckTimeout):
            await slave.wait_ready(timeout_s=0.05)

    @pytest.mark.asyncio
    async def test_transaction_helper(self):
        """EEPROM-style page-write pattern: wait then write."""
        adapter = MockAdapter()
        iface = Interface(adapter)
        slave = Slave(iface, addr=0x50)
        result = await slave.transaction(
            WaitAck(slave.addr, timeout_s=0.05),
            Transfer(slave.addr, data_w=b"\x00\xaa\xbb"),
        )
        assert result == (None, None)
        tx = adapter.transactions[0]
        assert isinstance(tx.items[0], WaitAck)
        assert isinstance(tx.items[1], Transfer)

    @pytest.mark.asyncio
    async def test_batching(self):
        """Multiple slave calls posted before await batch into one flush."""
        adapter = MockAdapter()
        iface = Interface(adapter)
        slave = Slave(iface, addr=0x50)
        f1 = slave.read(2)
        f2 = slave.write(b"\x01")
        f3 = slave.write_read(b"\x00", 4)
        r1, r2, r3 = await asyncio.gather(f1, f2, f3)
        assert r1 == bytes(2)
        assert r2 is None
        assert r3 == bytes(4)
        assert len(adapter.transactions) == 3

    def test_default_name(self):
        iface = Interface(MockAdapter())
        slave = Slave(iface, addr=0x50)
        assert "0x50" in slave.name

    def test_custom_name(self):
        iface = Interface(MockAdapter())
        slave = Slave(iface, addr=0x50, name="eeprom")
        assert slave.name == "eeprom"

    @pytest.mark.asyncio
    async def test_address_nack_propagates(self):
        adapter = MockAdapter()

        def nack(items):
            raise AddressNack(items[0].addr)
        adapter.responder = nack

        iface = Interface(adapter)
        slave = Slave(iface, addr=0x50)
        with pytest.raises(AddressNack):
            await slave.read(4)

    def test_repr(self):
        iface = Interface(MockAdapter())
        slave = Slave(iface, addr=0x50)
        assert "0x50" in repr(slave)
