import asyncio
import pytest
from crobe_async.protocol.swd import (
    Ack, Read, Write, Run, Wakeup, Interface,
    JtagToSwd, SwdToDormant, DormantToSwd, LineReset,
    DP_IDCODE, DP_ABORT, DP_CTRL_STAT, DP_SELECT, DP_RDBUFF,
)
from crobe_async.engine import Batcher


class TestAck:
    def test_ok(self):
        assert Ack.OK == 1

    def test_wait(self):
        assert Ack.WAIT == 2

    def test_error(self):
        assert Ack.ERROR == 4

    def test_parity_err(self):
        assert Ack.PARITY_ERR == 8


class TestReadOp:
    def test_dp_read(self):
        op = Read(ap=False, addr=0x00)
        assert not op.ap
        assert op.addr == 0x00
        assert op.data is None
        assert op.ack is None

    def test_ap_read(self):
        op = Read(ap=True, addr=0x04)
        assert op.ap
        assert op.addr == 0x04

    def test_cmd_byte_dp_idcode(self):
        # DP IDCODE read: ap=0, addr=0x00
        op = Read(ap=False, addr=0x00)
        # parity = 0 ^ 0 ^ 0 ^ 1 = 1
        # cmd = 0 | 0 | (1<<5) | 0x85 = 0xa5
        assert op.cmd == 0xa5

    def test_cmd_byte_ap_read(self):
        # AP read addr=0x00: ap=1, addr=0x00
        op = Read(ap=True, addr=0x00)
        # parity = 1 ^ 0 ^ 0 ^ 1 = 0
        # cmd = (1<<1) | 0 | 0 | 0x85 = 0x87
        assert op.cmd == 0x87

    def test_addr_masked(self):
        # Only bits [3:2] of addr matter
        op = Read(ap=False, addr=0xff)
        assert op.addr == 0x0c

    def test_repr(self):
        assert "DP" in repr(Read(ap=False, addr=0))
        assert "AP" in repr(Read(ap=True, addr=0))


class TestWriteOp:
    def test_dp_write(self):
        op = Write(ap=False, addr=0x00, data=0xdeadbeef)
        assert not op.ap
        assert op.data == 0xdeadbeef

    def test_ap_write(self):
        op = Write(ap=True, addr=0x04, data=0x12345678)
        assert op.ap
        assert op.data == 0x12345678

    def test_cmd_byte_dp_abort(self):
        # DP ABORT write: ap=0, addr=0x00
        op = Write(ap=False, addr=0x00, data=0)
        # parity = 0 ^ 0 ^ 0 = 0
        # cmd = 0 | 0 | 0 | 0x81 = 0x81
        assert op.cmd == 0x81

    def test_data_masked_32bit(self):
        op = Write(ap=False, addr=0, data=0x1ffffffff)
        assert op.data == 0xffffffff

    def test_repr(self):
        assert "DP" in repr(Write(ap=False, addr=0, data=0))


class TestRunOp:
    def test_basic(self):
        op = Run(10)
        assert op.cycles == 10
        assert "10" in repr(op)


class TestWakeupOp:
    def test_default(self):
        op = Wakeup()
        assert op.cycles == 50

    def test_custom(self):
        op = Wakeup(100)
        assert op.cycles == 100


class TestProtocolSequences:
    def test_jtag_to_swd(self):
        s = JtagToSwd()
        assert len(s.tms) == 71  # 50 + 16 + 5

    def test_swd_to_dormant(self):
        s = SwdToDormant()
        assert len(s.tms) == 66  # 50 + 16

    def test_dormant_to_swd(self):
        s = DormantToSwd()
        assert len(s.tms) == 190  # 50 + 128 + 4 + 8

    def test_line_reset(self):
        r = LineReset()
        assert len(r.tms) == 52  # 50 + 2
        # First 50 bits should be all 1s
        for i in range(50):
            assert r.tms[i] == True
        # Last 2 bits should be 0
        assert r.tms[50] == False
        assert r.tms[51] == False


class MockSwdAdapter(Batcher):
    """Records ops and populates Read.data."""

    def __init__(self):
        super().__init__()
        self.ops = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Read):
                op.data = 0x0ba00477
                op.ack = Ack.OK
            elif isinstance(op, Write):
                op.ack = Ack.OK
            future.set_result(op)


class TestInterface:
    @pytest.mark.asyncio
    async def test_passthrough_read(self):
        adapter = MockSwdAdapter()
        iface = Interface(adapter)
        op = Read(ap=False, addr=0x00)
        result = await iface.post(op)
        assert result is op
        assert op.data == 0x0ba00477
        assert len(adapter.ops) == 1

    @pytest.mark.asyncio
    async def test_passthrough_write(self):
        adapter = MockSwdAdapter()
        iface = Interface(adapter)
        op = Write(ap=False, addr=0x00, data=0x12345678)
        result = await iface.post(op)
        assert result is op
        assert op.ack == Ack.OK

    @pytest.mark.asyncio
    async def test_batching(self):
        adapter = MockSwdAdapter()
        iface = Interface(adapter)
        r = Read(ap=False, addr=0x00)
        w = Write(ap=False, addr=0x04, data=0)
        f1 = iface.post(r)
        f2 = iface.post(w)
        await asyncio.gather(f1, f2)
        assert len(adapter.ops) == 2

    @pytest.mark.asyncio
    async def test_run_forwarded(self):
        adapter = MockSwdAdapter()
        iface = Interface(adapter)
        op = Run(10)
        result = await iface.post(op)
        assert result is op
        assert adapter.ops[0].cycles == 10

    def test_repr(self):
        adapter = MockSwdAdapter()
        iface = Interface(adapter, name="swd0")
        assert "Interface" in repr(iface)


class TestDpAddresses:
    def test_constants(self):
        assert DP_IDCODE == 0x00
        assert DP_ABORT == 0x00
        assert DP_CTRL_STAT == 0x04
        assert DP_SELECT == 0x08
        assert DP_RDBUFF == 0x0c
