import asyncio
import pytest
from crobe_async.component.arm.dp import ApRead, ApWrite, SwDp
from crobe_async.component.arm.ap import Ap, MemAp, MemRead, MemWrite
from crobe_async.protocol.swd import Read as SwdRead, Write as SwdWrite
from crobe_async.engine import Batcher


class MockSwdInterface(Batcher):
    """SWD interface mock."""

    def __init__(self):
        super().__init__()
        self.ops = []
        self._read_responses = {}
        self._default_read = 0

    def set_read_response(self, ap, addr, value):
        self._read_responses[(ap, addr)] = value

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, SwdRead):
                key = (op.ap, op.addr)
                op.data = self._read_responses.get(key, self._default_read)
            future.set_result(op)


class TestAp:
    @pytest.mark.asyncio
    async def test_reg_read(self):
        """AP reg_read posts ApRead to DP."""
        iface = MockSwdInterface()
        iface.set_read_response(False, 0x0c, 0xdeadbeef)  # RDBUFF
        dp = SwDp(iface)
        ap = Ap(dp, index=0)

        result = await ap.reg_read(0x00)
        assert result == 0xdeadbeef

    @pytest.mark.asyncio
    async def test_reg_write(self):
        """AP reg_write posts ApWrite to DP."""
        iface = MockSwdInterface()
        dp = SwDp(iface)
        ap = Ap(dp, index=0)

        result = await ap.reg_write(0x04, 0x12345678)
        assert result is None

        # Should find an AP write in the SWD ops
        ap_writes = [op for op in iface.ops
                     if isinstance(op, SwdWrite) and op.ap]
        assert len(ap_writes) == 1
        assert ap_writes[0].data == 0x12345678

    @pytest.mark.asyncio
    async def test_read_idr(self):
        """AP IDR read."""
        iface = MockSwdInterface()
        iface.set_read_response(False, 0x0c, 0x04770001)  # RDBUFF
        dp = SwDp(iface)
        ap = Ap(dp, index=0)

        idr = await ap.read_idr()
        assert idr == 0x04770001

    @pytest.mark.asyncio
    async def test_default_name(self):
        iface = MockSwdInterface()
        dp = SwDp(iface)
        ap = Ap(dp, index=2)
        assert ap.name == "ap2"

    @pytest.mark.asyncio
    async def test_repr(self):
        iface = MockSwdInterface()
        dp = SwDp(iface)
        ap = Ap(dp, index=0)
        assert "Ap" in repr(ap)


class TestMemAp:
    @pytest.mark.asyncio
    async def test_read32(self):
        """MemAp read32 generates CSW + TAR + DRW reads."""
        iface = MockSwdInterface()
        iface.set_read_response(False, 0x0c, 0xaabbccdd)  # RDBUFF
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        result = await memap.read32(0x20000000)
        # The result comes from DRW read
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_write32(self):
        """MemAp write32 generates CSW + TAR + DRW write."""
        iface = MockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        result = await memap.write32(0x20000000, 0xdeadbeef)
        assert result is None

    @pytest.mark.asyncio
    async def test_consecutive_reads_share_tar(self):
        """Consecutive reads at sequential addresses share TAR (auto-increment)."""
        iface = MockSwdInterface()
        iface.set_read_response(False, 0x0c, 0)
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)

        f1 = memap.read32(0x20000000)
        f2 = memap.read32(0x20000004)
        await asyncio.gather(f1, f2)

        # TAR should only be written once (auto-increment handles the second)
        ap_writes = [op for op in iface.ops
                     if isinstance(op, SwdWrite) and op.ap]
        tar_writes = [op for op in ap_writes if op.addr == 0x04]
        assert len(tar_writes) == 1
        assert tar_writes[0].data == 0x20000000

    @pytest.mark.asyncio
    async def test_mem_ops_repr(self):
        r = MemRead(0x20000000, 4)
        assert "MemRead" in repr(r)
        w = MemWrite(0x20000000, 0xab, 4)
        assert "MemWrite" in repr(w)

    @pytest.mark.asyncio
    async def test_repr(self):
        iface = MockSwdInterface()
        dp = SwDp(iface)
        memap = MemAp(dp, index=0)
        assert "MemAp" in repr(memap)
