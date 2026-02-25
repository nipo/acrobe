import asyncio
import pytest
from crobe_async.protocol.spi import Cs, Shift, Target
from crobe_async.engine import Batcher
from crobe_async.bitstring import BitString


class MockSpiInterface(Batcher):
    """Records ops and populates Shift.miso with dummy data."""

    def __init__(self):
        super().__init__()
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


class TestTarget:
    @pytest.mark.asyncio
    async def test_shift(self):
        iface = MockSpiInterface()
        target = Target(iface, cs=0, mode=0)
        result = await target.shift(b"\x9f")
        # result is the Shift op
        assert isinstance(result, Shift)

    @pytest.mark.asyncio
    async def test_transaction(self):
        iface = MockSpiInterface()
        target = Target(iface, cs=0, mode=0)
        result = await target.transaction(b"\x9f\x00\x00")

        # Should have CS assert, Shift, CS deassert
        cs_ops = [op for op in iface.ops if isinstance(op, Cs)]
        shift_ops = [op for op in iface.ops if isinstance(op, Shift)]

        assert len(cs_ops) == 2
        assert cs_ops[0].value == 0  # CS assert
        assert cs_ops[1].value is None  # CS deassert
        assert len(shift_ops) == 1
        assert result == bytes(3)  # miso from mock

    @pytest.mark.asyncio
    async def test_transaction_mode(self):
        iface = MockSpiInterface()
        target = Target(iface, cs=1, mode=3)
        await target.transaction(b"\x00")

        cs_on = [op for op in iface.ops if isinstance(op, Cs) and op.value is not None][0]
        assert cs_on.value == 1
        assert cs_on.mode == 3

    @pytest.mark.asyncio
    async def test_repr(self):
        iface = MockSpiInterface()
        target = Target(iface, cs=0, mode=2)
        assert "cs=0" in repr(target)
        assert "mode=2" in repr(target)
