"""Tests for the address-space protocol (`acrobe.protocol.memory`)."""

import asyncio

import pytest

from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.protocol.memory import (
    BackgroundLowering, BulkFromRegister, Interface, OverwideDecomposition,
    PendingBlob, PreciseDecomposition, Read8, Read16, Read32, ReadBlob,
    RegisterFromBulk, UnsupportedAccess, Write8, Write16, Write32, WriteBlob,
)


# -- Chunking ------------------------------------------------------

class TestChunks:
    def test_aligned_words(self):
        assert PreciseDecomposition.chunks(0x1000, 8) == [
            (0, 0x1000, 4), (4, 0x1004, 4)]

    def test_zero_size(self):
        assert PreciseDecomposition.chunks(0x1000, 0) == []

    def test_odd_head(self):
        assert PreciseDecomposition.chunks(0x1001, 7) == [
            (0, 0x1001, 1), (1, 0x1002, 2), (3, 0x1004, 4)]

    def test_halfword_head(self):
        assert PreciseDecomposition.chunks(0x1002, 6) == [
            (0, 0x1002, 2), (2, 0x1004, 4)]

    def test_tail_halfword_and_byte(self):
        assert PreciseDecomposition.chunks(0x1000, 7) == [
            (0, 0x1000, 4), (4, 0x1004, 2), (6, 0x1006, 1)]

    def test_single_byte_at_odd_address(self):
        assert PreciseDecomposition.chunks(0x1003, 1) == [(0, 0x1003, 1)]

    def test_chunks_cover_range_exactly(self):
        for addr in range(0x1000, 0x1008):
            for size in range(0, 17):
                chunks = PreciseDecomposition.chunks(addr, size)
                assert sum(n for _, _, n in chunks) == size
                cursor = addr
                for offset, chunk_addr, n in chunks:
                    assert chunk_addr == cursor
                    assert offset == cursor - addr
                    assert chunk_addr % n == 0
                    cursor += n


# -- Decomposition strategies --------------------------------------

class TestPreciseDecomposition:
    def test_read_ops_exact(self):
        ops = PreciseDecomposition.read_ops(0x1001, 5)
        assert [op for op, _, _ in ops] == [
            Read8(0x1001), Read16(0x1002), Read16(0x1004)]
        assert [(o, n) for _, o, n in ops] == [(0, 1), (1, 2), (3, 2)]

    def test_read_ops_empty(self):
        assert PreciseDecomposition.read_ops(0x1000, 0) == []

    def test_never_reads_outside_range(self):
        for addr in range(0x1000, 0x1008):
            for size in range(1, 13):
                for op, offset, n in PreciseDecomposition.read_ops(
                        addr, size):
                    assert offset >= 0
                    assert offset + n <= size


class TestOverwideDecomposition:
    def test_aligned_range_matches_precise(self):
        ops = OverwideDecomposition.read_ops(0x1000, 8)
        assert [op for op, _, _ in ops] == [Read32(0x1000), Read32(0x1004)]
        assert [(o, n) for _, o, n in ops] == [(0, 4), (4, 4)]

    def test_unaligned_head_gets_negative_offset(self):
        ops = OverwideDecomposition.read_ops(0x1001, 5)
        assert [op for op, _, _ in ops] == [
            Read32(0x1000), Read32(0x1004)]
        assert [o for _, o, _ in ops] == [-1, 3]

    def test_single_byte_is_one_word(self):
        ops = OverwideDecomposition.read_ops(0x1003, 1)
        assert [op for op, _, _ in ops] == [Read32(0x1000)]
        assert ops[0][1] == -3

    def test_empty(self):
        assert OverwideDecomposition.read_ops(0x1000, 0) == []

    def test_covers_whole_range(self):
        for addr in range(0x1000, 0x1008):
            for size in range(1, 13):
                ops = OverwideDecomposition.read_ops(addr, size)
                first_offset = ops[0][1]
                last = ops[-1]
                assert first_offset <= 0
                assert last[1] + last[2] >= size

    def test_writes_stay_precise(self):
        ops = OverwideDecomposition.write_ops(0x1001, b"\xaa\xbb\xcc")
        assert [op for op, _, _ in ops] == [
            Write8(0x1001, 0xaa), Write16(0x1002, 0xccbb)]


class TestWriteOps:
    def test_word_payload_is_little_endian(self):
        ops = PreciseDecomposition.write_ops(
            0x1000, b"\x11\x22\x33\x44")
        assert [op for op, _, _ in ops] == [Write32(0x1000, 0x44332211)]

    def test_empty_payload(self):
        assert PreciseDecomposition.write_ops(0x1000, b"") == []


# -- PendingBlob ---------------------------------------------------

class TestPendingBlob:
    @pytest.mark.asyncio
    async def test_read_reassembly(self):
        loop = asyncio.get_running_loop()
        user = loop.create_future()
        pending = PendingBlob(user, 4, is_read=True)
        subs = []
        for offset, size_bytes in ((0, 2), (2, 2)):
            f = loop.create_future()
            pending.attach(offset, size_bytes, f)
            subs.append(f)
        subs[0].set_result(0xbbaa)
        assert not user.done()
        subs[1].set_result(0xddcc)
        assert await user == b"\xaa\xbb\xcc\xdd"

    @pytest.mark.asyncio
    async def test_read_clips_overwide_edges(self):
        loop = asyncio.get_running_loop()
        user = loop.create_future()
        # 2-byte blob starting one byte into a covering word.
        pending = PendingBlob(user, 2, is_read=True)
        f = loop.create_future()
        pending.attach(-1, 4, f)
        f.set_result(0xddccbbaa)
        assert await user == b"\xbb\xcc"

    @pytest.mark.asyncio
    async def test_write_resolves_none(self):
        loop = asyncio.get_running_loop()
        user = loop.create_future()
        pending = PendingBlob(user, 4, is_read=False)
        f = loop.create_future()
        pending.attach(0, 4, f)
        f.set_result(None)
        assert await user is None

    @pytest.mark.asyncio
    async def test_first_exception_wins(self):
        loop = asyncio.get_running_loop()
        user = loop.create_future()
        pending = PendingBlob(user, 8, is_read=True)
        subs = []
        for offset in (0, 4):
            f = loop.create_future()
            pending.attach(offset, 4, f)
            subs.append(f)
        subs[0].set_exception(IOError("bus fault"))
        subs[1].set_result(0)
        with pytest.raises(IOError, match="bus fault"):
            await user


# -- À-la-carte rejection -------------------------------------------

class BulkOnly(Interface, Batcher, Node):
    ops = Interface.BULK_OPS

    def __init__(self):
        Batcher.__init__(self)
        Node.__init__(self, "bulk-only")
        self.seen = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.seen.append(op)
            future.set_result(b"\x00" * op.size
                              if isinstance(op, ReadBlob) else None)


class RegisterOnly(Interface, Batcher, Node):
    ops = Interface.REGISTER_OPS

    def __init__(self):
        Batcher.__init__(self)
        Node.__init__(self, "register-only")

    async def flush_ops(self, batch):
        for _, future in batch:
            future.set_result(0)


class TestAlaCarte:
    @pytest.mark.asyncio
    async def test_bulk_only_rejects_register_family(self):
        node = BulkOnly()
        for call in (lambda: node.read8(0),
                     lambda: node.read16(0),
                     lambda: node.read32(0),
                     lambda: node.write8(0, 0),
                     lambda: node.write16(0, 0),
                     lambda: node.write32(0, 0)):
            with pytest.raises(UnsupportedAccess):
                call()

    @pytest.mark.asyncio
    async def test_bulk_only_serves_bulk(self):
        node = BulkOnly()
        assert await node.mem_read(0x10, 4) == b"\x00\x00\x00\x00"

    @pytest.mark.asyncio
    async def test_register_only_rejects_bulk_family(self):
        node = RegisterOnly()
        with pytest.raises(UnsupportedAccess):
            node.mem_read(0, 4)
        with pytest.raises(UnsupportedAccess):
            node.mem_write(0, b"\x00")

    def test_default_surface_accepts_nothing(self):
        class Nothing(Interface):
            pass

        with pytest.raises(UnsupportedAccess):
            Nothing().read32(0)


# -- BulkFromRegister ----------------------------------------------

class RecordingRegisterBus(BulkFromRegister, Batcher, Node):
    """Register-family bus over a flat bytearray; records the exact
    register-op stream its lowering was handed."""

    def __init__(self, decomposition=PreciseDecomposition, size=0x100):
        Batcher.__init__(self)
        Node.__init__(self, "recording")
        self.decomposition = decomposition
        self.storage = bytearray(size)
        self.lowered: list = []
        self.fail_at: int | None = None

    ops = Interface.REGISTER_OPS | Interface.BULK_OPS

    __WIDTH = {Read8: 1, Read16: 2, Read32: 4,
               Write8: 1, Write16: 2, Write32: 4}

    def lower_register_ops(self, batch):
        for op, future in batch:
            self.lowered.append(op)
            width = self.__WIDTH[type(op)]
            if self.fail_at is not None and op.addr == self.fail_at:
                if future is not None:
                    future.set_exception(IOError("fault"))
                continue
            if isinstance(op, (Read8, Read16, Read32)):
                value = int.from_bytes(
                    self.storage[op.addr:op.addr + width], "little")
                if future is not None:
                    future.set_result(value)
            else:
                self.storage[op.addr:op.addr + width] = op.data.to_bytes(
                    width, "little")
                if future is not None:
                    future.set_result(None)


class TestBulkFromRegister:
    @pytest.mark.asyncio
    async def test_precise_read(self):
        bus = RecordingRegisterBus()
        bus.storage[0x10:0x18] = bytes(range(0x10, 0x18))
        assert await bus.mem_read(0x11, 5) == bytes(range(0x11, 0x16))
        assert bus.lowered == [Read8(0x11), Read16(0x12), Read16(0x14)]

    @pytest.mark.asyncio
    async def test_overwide_read(self):
        bus = RecordingRegisterBus(decomposition=OverwideDecomposition)
        bus.storage[0x10:0x18] = bytes(range(0x10, 0x18))
        assert await bus.mem_read(0x11, 5) == bytes(range(0x11, 0x16))
        assert bus.lowered == [Read32(0x10), Read32(0x14)]

    @pytest.mark.asyncio
    async def test_zero_size_read(self):
        bus = RecordingRegisterBus()
        assert await bus.mem_read(0x10, 0) == b""
        assert bus.lowered == []

    @pytest.mark.asyncio
    async def test_empty_write(self):
        bus = RecordingRegisterBus()
        assert await bus.mem_write(0x10, b"") is None
        assert bus.lowered == []

    @pytest.mark.asyncio
    async def test_write_roundtrip(self):
        bus = RecordingRegisterBus()
        await bus.mem_write(0x11, b"\xaa\xbb\xcc\xdd\xee")
        assert bytes(bus.storage[0x11:0x16]) == b"\xaa\xbb\xcc\xdd\xee"
        assert bus.lowered == [
            Write8(0x11, 0xaa), Write16(0x12, 0xccbb), Write16(0x14, 0xeedd)]

    @pytest.mark.asyncio
    async def test_write_is_precise_even_when_reads_are_overwide(self):
        bus = RecordingRegisterBus(decomposition=OverwideDecomposition)
        await bus.mem_write(0x11, b"\xaa")
        assert bus.lowered == [Write8(0x11, 0xaa)]

    @pytest.mark.asyncio
    async def test_sub_op_failure_surfaces_on_blob(self):
        bus = RecordingRegisterBus()
        bus.fail_at = 0x14
        with pytest.raises(IOError, match="fault"):
            await bus.mem_read(0x10, 8)

    @pytest.mark.asyncio
    async def test_register_ops_pass_through_untouched(self):
        bus = RecordingRegisterBus()
        bus.storage[0x20:0x24] = b"\x01\x02\x03\x04"
        assert await bus.read32(0x20) == 0x04030201
        assert bus.lowered == [Read32(0x20)]

    @pytest.mark.asyncio
    async def test_blob_and_register_ops_keep_batch_order(self):
        bus = RecordingRegisterBus()
        futures = [bus.read8(0x00), bus.mem_read(0x10, 4), bus.read8(0x30)]
        await asyncio.gather(*futures)
        assert bus.lowered == [Read8(0x00), Read32(0x10), Read8(0x30)]

    @pytest.mark.asyncio
    async def test_post_no_wait_blob_still_lowers(self):
        bus = RecordingRegisterBus()
        bus.post_no_wait(WriteBlob(0x10, b"\x01\x02\x03\x04"))
        await bus.mem_read(0x10, 4)
        assert bytes(bus.storage[0x10:0x14]) == b"\x01\x02\x03\x04"

    def test_lower_register_ops_is_abstract(self):
        class Bare(BulkFromRegister):
            pass

        with pytest.raises(NotImplementedError):
            Bare().lower_register_ops([])


# -- RegisterFromBulk ----------------------------------------------

class BlobBackedRegisters(RegisterFromBulk, Batcher, Node):
    ops = Interface.BULK_OPS

    def __init__(self):
        Batcher.__init__(self)
        Node.__init__(self, "blob-registers")
        self.storage = bytearray(0x100)
        self.posted: list = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.posted.append(op)
            if isinstance(op, ReadBlob):
                future.set_result(
                    bytes(self.storage[op.addr:op.addr + op.size]))
            else:
                self.storage[op.addr:op.addr + len(op.data)] = op.data
                future.set_result(None)


class TestRegisterFromBulk:
    @pytest.mark.asyncio
    async def test_read_widths(self):
        node = BlobBackedRegisters()
        node.storage[0x10:0x14] = b"\x11\x22\x33\x44"
        assert await node.read8(0x10) == 0x11
        assert await node.read16(0x10) == 0x2211
        assert await node.read32(0x10) == 0x44332211
        assert node.posted == [
            ReadBlob(0x10, 1), ReadBlob(0x10, 2), ReadBlob(0x10, 4)]

    @pytest.mark.asyncio
    async def test_write_widths(self):
        node = BlobBackedRegisters()
        await node.write8(0x10, 0xaa)
        await node.write16(0x12, 0xbbcc)
        await node.write32(0x14, 0x11223344)
        assert bytes(node.storage[0x10:0x18]) == (
            b"\xaa\x00\xcc\xbb\x44\x33\x22\x11")

    @pytest.mark.asyncio
    async def test_write_masks_to_width(self):
        node = BlobBackedRegisters()
        await node.write8(0x10, 0x1ff)
        assert node.storage[0x10] == 0xff


# -- BackgroundLowering --------------------------------------------

class SlowBackend(BackgroundLowering, Batcher, Node):
    ops = Interface.BULK_OPS

    def __init__(self):
        Batcher.__init__(self)
        Node.__init__(self, "slow")
        self.order: list = []
        self.raise_on: object = None
        self.leave_unresolved = False

    async def flush_ops(self, batch):
        self.dispatch(batch)

    async def run_ops(self, batch):
        await asyncio.sleep(0.01)
        for op, future in batch:
            self.order.append(op)
            if self.raise_on is not None and op == self.raise_on:
                raise IOError("backend down")
            if self.leave_unresolved:
                continue
            future.set_result(None)


class TestBackgroundLowering:
    @pytest.mark.asyncio
    async def test_flush_ops_does_not_block(self):
        node = SlowBackend()
        f = node.post(WriteBlob(0x10, b"\x01"))
        # The flush task ran, but the backend is still sleeping.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not f.done()
        await f

    @pytest.mark.asyncio
    async def test_batches_keep_submission_order(self):
        node = SlowBackend()
        first = node.post(WriteBlob(0x10, b"\x01"))
        await asyncio.sleep(0)
        second = node.post(WriteBlob(0x20, b"\x02"))
        await asyncio.gather(first, second)
        assert node.order == [
            WriteBlob(0x10, b"\x01"), WriteBlob(0x20, b"\x02")]

    @pytest.mark.asyncio
    async def test_backend_exception_reaches_every_future(self):
        node = SlowBackend()
        node.raise_on = WriteBlob(0x10, b"\x01")
        with pytest.raises(IOError, match="backend down"):
            await node.post(WriteBlob(0x10, b"\x01"))

    @pytest.mark.asyncio
    async def test_unresolved_future_fails_loudly(self):
        node = SlowBackend()
        node.leave_unresolved = True
        with pytest.raises(RuntimeError, match="unresolved"):
            await node.post(WriteBlob(0x10, b"\x01"))

    @pytest.mark.asyncio
    async def test_run_ops_is_abstract(self):
        class Bare(BackgroundLowering):
            pass

        with pytest.raises(NotImplementedError):
            await Bare().run_ops([])
