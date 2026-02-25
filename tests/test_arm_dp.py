import asyncio
import pytest
from crobe_async.component.arm.dp import (
    ApRead, ApWrite, DpRead, DpWrite, Run,
    SwDp, DpAccessFailure,
)
from crobe_async.protocol.swd import Read as SwdRead, Write as SwdWrite, Run as SwdRun
from crobe_async.engine import Batcher


class MockSwdInterface(Batcher):
    """Mock SWD interface that records ops and provides configurable responses."""

    def __init__(self):
        super().__init__()
        self.ops = []
        self._read_responses = {}  # (ap, addr) -> value or default
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


class TestDpCommands:
    def test_ap_read_repr(self):
        op = ApRead(0, 0x00)
        assert "ApRead" in repr(op)

    def test_ap_write_repr(self):
        op = ApWrite(0, 0x04, 0xdeadbeef)
        assert "ApWrite" in repr(op)

    def test_dp_read_repr(self):
        op = DpRead(0x04)
        assert "DpRead" in repr(op)

    def test_dp_write_repr(self):
        op = DpWrite(0x04, 0x12345678)
        assert "DpWrite" in repr(op)

    def test_run_repr(self):
        op = Run(10)
        assert "10" in repr(op)


class TestSwDpBasics:
    @pytest.mark.asyncio
    async def test_construction(self):
        iface = MockSwdInterface()
        dp = SwDp(iface)
        assert dp._select is None

    @pytest.mark.asyncio
    async def test_ap_read_single(self):
        """Single AP read generates AP Read + RDBUFF."""
        iface = MockSwdInterface()
        iface.set_read_response(True, 0x00, 0)  # AP read (initiates)
        iface.set_read_response(False, 0x0c, 0x12345678)  # RDBUFF
        dp = SwDp(iface)

        result = await dp.post(ApRead(0, 0x00))
        assert result == 0x12345678

        # Should have: SELECT write + AP Read + RDBUFF
        swd_ops = iface.ops
        select_writes = [op for op in swd_ops
                         if isinstance(op, SwdWrite) and op.addr == 0x08]
        ap_reads = [op for op in swd_ops
                    if isinstance(op, SwdRead) and op.ap]
        rdbuff_reads = [op for op in swd_ops
                        if isinstance(op, SwdRead) and not op.ap and op.addr == 0x0c]

        assert len(select_writes) == 1
        assert len(ap_reads) == 1
        assert len(rdbuff_reads) == 1

    @pytest.mark.asyncio
    async def test_ap_write(self):
        """AP write generates SELECT + AP Write."""
        iface = MockSwdInterface()
        dp = SwDp(iface)

        result = await dp.post(ApWrite(0, 0x04, 0xdeadbeef))
        assert result is None

        swd_ops = iface.ops
        select_writes = [op for op in swd_ops
                         if isinstance(op, SwdWrite) and op.addr == 0x08]
        ap_writes = [op for op in swd_ops
                     if isinstance(op, SwdWrite) and op.ap]

        assert len(select_writes) == 1
        assert len(ap_writes) == 1
        assert ap_writes[0].data == 0xdeadbeef

    @pytest.mark.asyncio
    async def test_dp_read(self):
        """DP read is a direct SWD read."""
        iface = MockSwdInterface()
        iface.set_read_response(False, 0x04, 0xaabbccdd)
        dp = SwDp(iface)

        result = await dp.post(DpRead(0x04))
        assert result == 0xaabbccdd

    @pytest.mark.asyncio
    async def test_dp_write(self):
        """DP write is a direct SWD write."""
        iface = MockSwdInterface()
        dp = SwDp(iface)

        result = await dp.post(DpWrite(0x04, 0x11223344))
        assert result is None

        dp_writes = [op for op in iface.ops
                     if isinstance(op, SwdWrite) and not op.ap]
        assert any(op.data == 0x11223344 for op in dp_writes)

    @pytest.mark.asyncio
    async def test_run(self):
        """Run generates SWD Run."""
        iface = MockSwdInterface()
        dp = SwDp(iface)

        await dp.post(Run(10))
        run_ops = [op for op in iface.ops if isinstance(op, SwdRun)]
        assert len(run_ops) == 1
        assert run_ops[0].cycles == 10


class TestSwDpPipelining:
    @pytest.mark.asyncio
    async def test_two_ap_reads_pipeline(self):
        """Two consecutive AP reads share the pipeline."""
        iface = MockSwdInterface()
        # First AP read: data comes from second AP read's SWD response
        iface.set_read_response(True, 0x00, 0xAAAAAAAA)
        # Second read captures first read's data
        iface.set_read_response(True, 0x04, 0xBBBBBBBB)
        # RDBUFF captures second read's data
        iface.set_read_response(False, 0x0c, 0xCCCCCCCC)

        dp = SwDp(iface)

        f1 = dp.post(ApRead(0, 0x00))
        f2 = dp.post(ApRead(0, 0x04))
        r1, r2 = await asyncio.gather(f1, f2)

        # r1 gets data from the 2nd SWD AP read (which captures 1st AP read's data)
        # r2 gets data from RDBUFF
        # In our mock, the second AP read returns 0xBBBBBBBB
        # and RDBUFF returns 0xCCCCCCCC
        assert r1 == 0xBBBBBBBB  # from 2nd SWD Read
        assert r2 == 0xCCCCCCCC  # from RDBUFF

    @pytest.mark.asyncio
    async def test_read_write_read_pipeline(self):
        """AP read, AP write, AP read: write flushes pending read."""
        iface = MockSwdInterface()
        iface.set_read_response(True, 0x00, 0)
        iface.set_read_response(False, 0x0c, 0x11111111)  # RDBUFF for first read

        dp = SwDp(iface)

        f1 = dp.post(ApRead(0, 0x00))
        f2 = dp.post(ApWrite(0, 0x04, 0xbeef))
        r1, r2 = await asyncio.gather(f1, f2)

        # f1 should be resolved by RDBUFF (inserted before write)
        assert r1 == 0x11111111
        assert r2 is None


class TestSwDpSelectTracking:
    @pytest.mark.asyncio
    async def test_select_updates_on_ap_change(self):
        """SELECT is updated when AP index changes."""
        iface = MockSwdInterface()
        dp = SwDp(iface)

        f1 = dp.post(ApWrite(0, 0x00, 0))
        f2 = dp.post(ApWrite(1, 0x00, 0))
        await asyncio.gather(f1, f2)

        select_writes = [op for op in iface.ops
                         if isinstance(op, SwdWrite) and op.addr == 0x08]
        assert len(select_writes) == 2
        # First SELECT: AP0, bank 0x00
        assert select_writes[0].data == (0 << 24) | 0x00
        # Second SELECT: AP1, bank 0x00
        assert select_writes[1].data == (1 << 24) | 0x00

    @pytest.mark.asyncio
    async def test_select_no_update_same_ap(self):
        """SELECT is not re-written if AP and bank haven't changed."""
        iface = MockSwdInterface()
        dp = SwDp(iface)

        f1 = dp.post(ApWrite(0, 0x00, 0))
        f2 = dp.post(ApWrite(0, 0x04, 0))
        await asyncio.gather(f1, f2)

        select_writes = [op for op in iface.ops
                         if isinstance(op, SwdWrite) and op.addr == 0x08]
        # Same AP and bank (both addr & 0xf0 == 0x00), so only one SELECT
        assert len(select_writes) == 1

    @pytest.mark.asyncio
    async def test_select_updates_on_bank_change(self):
        """SELECT is updated when register bank changes."""
        iface = MockSwdInterface()
        dp = SwDp(iface)

        # addr 0x00 → bank 0x00, addr 0xfc → bank 0xf0
        f1 = dp.post(ApWrite(0, 0x00, 0))
        f2 = dp.post(ApWrite(0, 0xfc, 0))
        await asyncio.gather(f1, f2)

        select_writes = [op for op in iface.ops
                         if isinstance(op, SwdWrite) and op.addr == 0x08]
        assert len(select_writes) == 2
