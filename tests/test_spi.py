import asyncio
import pytest
from acrobe.protocol.spi import Cs, Shift, Target, Interface
from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.bitstring import BitString


class MockSpiAdapter(Batcher, Node):
    """Records ops and populates Shift.miso with dummy data."""

    def __init__(self, name: str = "mock-spi"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.ops = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Shift) and op.read_miso:
                op.miso = bytes(op.byte_count)
            future.set_result(op)


class TestCsOp:
    def test_select(self):
        cs = Cs(0, mode=1)
        assert cs.value == 0
        assert cs.mode == 1
        assert "Cs 0" in repr(cs)

    def test_deselect(self):
        cs = Cs(None)
        assert cs.value is None
        assert "None" in repr(cs)

    def test_default_mode(self):
        cs = Cs(0)
        assert cs.mode == 0


class TestShiftOp:
    def test_bytes_input(self):
        op = Shift(b"\xaa\xbb", read_miso=True)
        assert op.mosi == b"\xaa\xbb"
        assert op.byte_count == 2
        assert op.read_miso is True
        assert op.miso is None

    def test_int_input(self):
        op = Shift(4, read_miso=True)
        assert op.byte_count == 4
        assert op.mosi == bytes(4)

    def test_bitstring_input(self):
        bs = BitString(0xab, 8)
        op = Shift(bs)
        assert op.byte_count == 1

    def test_write_only(self):
        op = Shift(b"\x01\x02", read_miso=False)
        assert op.read_miso is False

    def test_repr(self):
        op = Shift(b"\x01", read_miso=True)
        assert "1B" in repr(op)
        assert "True" in repr(op)


class TestInterface:
    @pytest.mark.asyncio
    async def test_passthrough(self):
        adapter = MockSpiAdapter()
        iface = Interface(adapter)
        shift = Shift(b"\x9f", read_miso=True)
        result = await iface.post(shift)
        assert result is shift
        assert shift.miso == bytes(1)
        assert len(adapter.ops) == 1

    @pytest.mark.asyncio
    async def test_batching(self):
        adapter = MockSpiAdapter()
        iface = Interface(adapter)
        s1 = Shift(b"\x01", read_miso=False)
        s2 = Shift(b"\x02", read_miso=True)
        f1 = iface.post(s1)
        f2 = iface.post(s2)
        await asyncio.gather(f1, f2)
        assert len(adapter.ops) == 2

    @pytest.mark.asyncio
    async def test_cs_forwarded(self):
        adapter = MockSpiAdapter()
        iface = Interface(adapter)
        cs = Cs(0, mode=1)
        result = await iface.post(cs)
        assert result is cs
        assert len(adapter.ops) == 1
        assert adapter.ops[0] is cs

    def test_repr(self):
        adapter = MockSpiAdapter()
        iface = Interface(adapter, name="bus0")
        assert "Interface" in repr(iface)


class TestTarget:
    @pytest.mark.asyncio
    async def test_transaction_single_shift(self):
        adapter = MockSpiAdapter()
        iface = Interface(adapter)
        target = Target(iface, cs=0, mode=0)
        shift = Shift(b"\x9f\x00\x00", read_miso=True)
        result = await target.transaction(shift)

        # Should have CS assert, Shift, CS deassert
        cs_ops = [op for op in adapter.ops if isinstance(op, Cs)]
        shift_ops = [op for op in adapter.ops if isinstance(op, Shift)]

        assert len(cs_ops) == 2
        assert cs_ops[0].value == 0  # CS assert
        assert cs_ops[1].value is None  # CS deassert
        assert len(shift_ops) == 1
        assert result == (shift,)

    @pytest.mark.asyncio
    async def test_transaction_multiple_shifts(self):
        adapter = MockSpiAdapter()
        iface = Interface(adapter)
        target = Target(iface, cs=0, mode=0)
        s1 = Shift(b"\x9f", read_miso=False)
        s2 = Shift(3, read_miso=True)
        result = await target.transaction(s1, s2)

        cs_ops = [op for op in adapter.ops if isinstance(op, Cs)]
        shift_ops = [op for op in adapter.ops if isinstance(op, Shift)]

        assert len(cs_ops) == 2
        assert len(shift_ops) == 2
        assert result == (s1, s2)
        assert s2.miso == bytes(3)

    @pytest.mark.asyncio
    async def test_transaction_mode(self):
        adapter = MockSpiAdapter()
        iface = Interface(adapter)
        target = Target(iface, cs=1, mode=3)
        await target.transaction(Shift(b"\x00", read_miso=False))

        cs_on = [op for op in adapter.ops if isinstance(op, Cs) and op.value is not None][0]
        assert cs_on.value == 1
        assert cs_on.mode == 3

    @pytest.mark.asyncio
    async def test_transaction_batching(self):
        """Multiple transactions posted before await should batch."""
        adapter = MockSpiAdapter()
        iface = Interface(adapter)
        target = Target(iface, cs=0, mode=0)
        f1 = target.transaction(Shift(b"\x01", read_miso=False))
        f2 = target.transaction(Shift(b"\x02", read_miso=False))
        await asyncio.gather(f1, f2)

        cs_ops = [op for op in adapter.ops if isinstance(op, Cs)]
        # Two transactions = 4 CS ops (2 per transaction)
        assert len(cs_ops) == 4

    @pytest.mark.asyncio
    async def test_repr(self):
        adapter = MockSpiAdapter()
        iface = Interface(adapter)
        target = Target(iface, cs=0, mode=2)
        assert "cs=0" in repr(target)
        assert "mode=2" in repr(target)
