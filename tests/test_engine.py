import asyncio
import pytest
from crobe_async.engine import Batcher


class Accumulator(Batcher):
    """Test batcher that accumulates ops and resolves futures with the op."""

    def __init__(self):
        super().__init__()
        self.flush_count = 0
        self.batches = []

    async def flush_ops(self, batch):
        self.flush_count += 1
        self.batches.append([(op, future) for op, future in batch])
        for op, future in batch:
            future.set_result(op)


class FailingBatcher(Batcher):
    async def flush_ops(self, batch):
        raise RuntimeError("flush failed")


class LowerLayer(Batcher):
    """Simulates a lower layer that doubles the value."""

    async def flush_ops(self, batch):
        for op, future in batch:
            future.set_result(op * 2)


class UpperLayer(Batcher):
    """Simulates an upper layer that translates ops to the lower layer."""

    def __init__(self, lower):
        super().__init__()
        self.lower = lower

    async def flush_ops(self, batch):
        # Translate each op by posting to lower layer
        lower_futures = [(op, future, self.lower.post(op)) for op, future in batch]
        # Await all lower futures
        for op, future, lower_future in lower_futures:
            result = await lower_future
            future.set_result(result)


class TestBasicBatching:
    @pytest.mark.asyncio
    async def test_single_post(self):
        b = Accumulator()
        f = b.post("op1")
        result = await f
        assert result == "op1"
        assert b.flush_count == 1

    @pytest.mark.asyncio
    async def test_multiple_posts_batch(self):
        b = Accumulator()
        f1 = b.post("op1")
        f2 = b.post("op2")
        f3 = b.post("op3")
        r1, r2, r3 = await asyncio.gather(f1, f2, f3)
        assert r1 == "op1"
        assert r2 == "op2"
        assert r3 == "op3"
        # All posted before any await, so should batch into one flush
        assert b.flush_count == 1

    @pytest.mark.asyncio
    async def test_sequential_batches(self):
        b = Accumulator()
        f1 = b.post("op1")
        await f1
        f2 = b.post("op2")
        await f2
        # Two separate batches
        assert b.flush_count == 2

    @pytest.mark.asyncio
    async def test_fire_and_forget(self):
        b = Accumulator()
        b.post("op1")
        b.post("op2")
        # Don't await, but let event loop run
        await asyncio.sleep(0)
        assert b.flush_count == 1
        assert len(b.batches[0]) == 2


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_flush_error_propagates(self):
        b = FailingBatcher()
        f = b.post("op1")
        with pytest.raises(RuntimeError, match="flush failed"):
            await f

    @pytest.mark.asyncio
    async def test_error_affects_all_in_batch(self):
        b = FailingBatcher()
        f1 = b.post("op1")
        f2 = b.post("op2")
        with pytest.raises(RuntimeError):
            await f1
        with pytest.raises(RuntimeError):
            await f2


class TestCascading:
    @pytest.mark.asyncio
    async def test_two_layer_cascade(self):
        lower = LowerLayer()
        upper = UpperLayer(lower)
        f = upper.post(5)
        result = await f
        assert result == 10  # 5 * 2 from lower layer

    @pytest.mark.asyncio
    async def test_cascade_batching(self):
        lower = LowerLayer()
        upper = UpperLayer(lower)
        f1 = upper.post(3)
        f2 = upper.post(7)
        r1, r2 = await asyncio.gather(f1, f2)
        assert r1 == 6
        assert r2 == 14


class TestReentrancy:
    @pytest.mark.asyncio
    async def test_posts_during_flush(self):
        """Ops posted during flush_ops are processed in the next iteration."""
        results = []

        class ReentrantBatcher(Batcher):
            def __init__(self):
                super().__init__()
                self.call_count = 0

            async def flush_ops(self, batch):
                self.call_count += 1
                for op, future in batch:
                    if op == "trigger":
                        # Post more ops during flush
                        self.post("spawned")
                    future.set_result(op)

        b = ReentrantBatcher()
        f = b.post("trigger")
        await f
        # Let the spawned op flush
        await asyncio.sleep(0)
        assert b.call_count == 2
