import asyncio
import pytest

from acrobe.protocol.datagram import Send, Recv
from acrobe.component.nsl.bnoc.framed import Framed, JtagFramed
from acrobe.component.nsl.bnoc.routed import Router, Route, Context, FramedEndpoint
from acrobe.component.nsl.bnoc.committed import Committed


# -- Mock FIFO for JtagFramed tests --

class MockFifo:
    """Captures tx_words, returns canned rx frames."""

    def __init__(self, rx_frames=None):
        self.tx_words = []
        self._rx_frames = rx_frames or []

    async def exchange(self, tx_words, expect_frames=1):
        self.tx_words.extend(tx_words)
        # Flatten all canned rx frames into a single word list
        rx = []
        for frame in self._rx_frames[:expect_frames]:
            rx.extend(Framed.encode(frame))
        return rx


# -- Mock Framed for Router/Committed tests --

class MockFramed(Framed):
    """In-memory Framed that captures sent data and returns canned responses."""

    def __init__(self, rx_frames=None, name: str = "mock-framed"):
        super().__init__(name)
        self.sent = []
        self._rx_frames = list(rx_frames or [])

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                self.sent.append(op.data)
                future.set_result(None)
            elif isinstance(op, Recv):
                frame = self._rx_frames.pop(0)
                future.set_result((frame, None))


# -- Framed base class tests --

class TestFramedEncodeDecode:
    def test_encode_sets_last_on_final(self):
        words = Framed.encode(b'\x01\x02\x03')
        assert words == [0x01, 0x02, 0x103]

    def test_encode_single_byte(self):
        words = Framed.encode(b'\xAB')
        assert words == [0x1AB]

    def test_encode_empty(self):
        assert Framed.encode(b'') == []

    def test_decode_strips_last(self):
        data = Framed.decode([0x01, 0x02, 0x103])
        assert data == b'\x01\x02\x03'

    def test_roundtrip(self):
        original = b'\xDE\xAD\xBE\xEF'
        encoded = Framed.encode(original)
        decoded = Framed.decode(encoded)
        assert decoded == original

    def test_split_frames(self):
        words = [0x01, 0x102, 0x03, 0x04, 0x105]
        frames = Framed.split_frames(words)
        assert len(frames) == 2
        assert frames[0] == [0x01, 0x102]
        assert frames[1] == [0x03, 0x04, 0x105]

    def test_split_frames_single(self):
        words = [0x01, 0x02, 0x103]
        frames = Framed.split_frames(words)
        assert len(frames) == 1
        assert frames[0] == [0x01, 0x02, 0x103]


class TestFramedInheritance:
    def test_jtag_framed_is_framed(self):
        fifo = MockFifo()
        jf = JtagFramed(fifo)
        assert isinstance(jf, Framed)

    def test_route_is_framed(self):
        mf = MockFramed()
        router = Router(mf)
        route = router.route(0, 1)
        assert isinstance(route, Framed)

    def test_committed_is_framed(self):
        mf = MockFramed()
        c = Committed(mf)
        assert isinstance(c, Framed)


# -- JtagFramed tests --

class TestJtagFramed:
    @pytest.mark.asyncio
    async def test_send_recv(self):
        rx_data = b'\xAA\xBB'
        fifo = MockFifo(rx_frames=[rx_data])
        jf = JtagFramed(fifo)

        tx_data = b'\x01\x02\x03'
        jf.send(tx_data)
        data, _ = await jf.recv()

        assert data == rx_data
        assert fifo.tx_words == Framed.encode(tx_data)

    @pytest.mark.asyncio
    async def test_multiple_frames(self):
        fifo = MockFifo(rx_frames=[b'\x10\x11', b'\x20\x21\x22'])
        jf = JtagFramed(fifo)

        jf.send(b'\x01')
        r1 = jf.recv()
        r2 = jf.recv()
        (data1, _), (data2, _) = await asyncio.gather(r1, r2)

        assert data1 == b'\x10\x11'
        assert data2 == b'\x20\x21\x22'

    @pytest.mark.asyncio
    async def test_send_only(self):
        fifo = MockFifo()
        jf = JtagFramed(fifo)
        result = await jf.send(b'\x55')
        assert result is None
        assert fifo.tx_words == Framed.encode(b'\x55')

    @pytest.mark.asyncio
    async def test_empty_frame(self):
        fifo = MockFifo(rx_frames=[b'\x00'])
        jf = JtagFramed(fifo)
        data, _ = await jf.recv()
        assert data == b'\x00'


# -- Router / Route tests --

class TestRouter:
    @pytest.mark.asyncio
    async def test_routing_header_encoding(self):
        """Routing header: dst[3:0] | src[7:4]."""
        # Inbound frame: from remote=5 to local=0xf
        inbound_ctx = Context(0xf, 5)
        mf = MockFramed(rx_frames=[
            bytes([inbound_ctx.header()]) + b'\xBB',
        ])
        router = Router(mf)
        route = router.route(0xf, 5)

        route.send(b'\xAA')
        data, _ = await route.recv()

        assert data == b'\xBB'
        # Sent frame should have header byte prepended (outbound: dst=5, src=0xf)
        assert len(mf.sent) == 1
        header = mf.sent[0][0]
        assert header & 0xf == 5      # destination
        assert header >> 4 == 0xf     # source

    @pytest.mark.asyncio
    async def test_out_of_order_dispatch(self):
        """Responses arrive B then A, both routes get correct data."""
        # Inbound contexts: device sends TO local FROM remote
        inbound_a = Context(0xf, 0xa)  # from remote 0xa to local 0xf
        inbound_b = Context(0xf, 0xb)  # from remote 0xb to local 0xf

        mf = MockFramed(rx_frames=[
            bytes([inbound_b.header()]) + b'\x22',  # B arrives first
            bytes([inbound_a.header()]) + b'\x11',  # A arrives second
        ])
        router = Router(mf)
        route_a = router.route(0xf, 0xa)
        route_b = router.route(0xf, 0xb)

        route_a.send(b'\x01')
        route_b.send(b'\x02')
        recv_a = route_a.recv()
        recv_b = route_b.recv()

        (data_a, _), (data_b, _) = await asyncio.gather(recv_a, recv_b)
        assert data_a == b'\x11'
        assert data_b == b'\x22'

    @pytest.mark.asyncio
    async def test_recv_only(self):
        """Route.recv() without send(), frame arrives and resolves."""
        inbound = Context(0xf, 3)  # from remote 3 to local 0xf
        mf = MockFramed(rx_frames=[
            bytes([inbound.header()]) + b'\xCC',
        ])
        router = Router(mf)
        route = router.route(0xf, 3)

        data, _ = await route.recv()
        assert data == b'\xCC'

    @pytest.mark.asyncio
    async def test_buffered_frames(self):
        """Frame arrives for context with no pending recv, gets buffered."""
        inbound_a = Context(0xf, 1)  # from remote 1 to local 0xf
        inbound_b = Context(0xf, 2)  # from remote 2 to local 0xf

        mf = MockFramed(rx_frames=[
            bytes([inbound_a.header()]) + b'\x11',  # For route A
            bytes([inbound_b.header()]) + b'\x22',  # For route B
        ])
        router = Router(mf)
        route_b = router.route(0xf, 2)

        # Only recv on route B — frame for route A will be buffered
        data_b, _ = await route_b.recv()
        assert data_b == b'\x22'

        # Now recv on route A — should get buffered frame
        route_a = router.route(0xf, 1)
        data_a, _ = await route_a.recv()
        assert data_a == b'\x11'


# -- FramedEndpoint tests --

class TestFramedEndpoint:
    @pytest.mark.asyncio
    async def test_tag_correlation(self):
        """Tag auto-increments and is checked in response."""
        # Inbound: from remote=1 to local=0xf
        inbound = Context(0xf, 1)

        mf = MockFramed(rx_frames=[
            bytes([inbound.header(), 0x00]) + b'\xAA',  # tag 0
        ])
        router = Router(mf)
        route = router.route(0xf, 1)
        ep = FramedEndpoint(route)

        result = await ep.transact(b'\x55')
        assert result == b'\xAA'

    @pytest.mark.asyncio
    async def test_tag_increment(self):
        inbound = Context(0xf, 1)
        mf = MockFramed(rx_frames=[
            bytes([inbound.header(), 0x00]) + b'\xAA',
            bytes([inbound.header(), 0x01]) + b'\xBB',
        ])
        router = Router(mf)
        route = router.route(0xf, 1)
        ep = FramedEndpoint(route)

        r1 = await ep.transact(b'\x11')
        r2 = await ep.transact(b'\x22')
        assert r1 == b'\xAA'
        assert r2 == b'\xBB'

    @pytest.mark.asyncio
    async def test_tag_mismatch_raises(self):
        inbound = Context(0xf, 1)
        mf = MockFramed(rx_frames=[
            bytes([inbound.header(), 0xFF]) + b'\xAA',  # wrong tag
        ])
        router = Router(mf)
        route = router.route(0xf, 1)
        ep = FramedEndpoint(route)

        with pytest.raises(RuntimeError, match="Tag mismatch"):
            await ep.transact(b'\x55')


# -- Committed tests --

class TestCommitted:
    @pytest.mark.asyncio
    async def test_commit_byte_appended(self):
        mf = MockFramed(rx_frames=[b'\xAA'])
        c = Committed(mf)

        c.send(b'\x01\x02')
        data, _ = await c.recv()

        assert data == b'\xAA'
        # Check that send added commit byte
        assert mf.sent[0] == b'\x01\x02\x01'  # COMMIT = 0x01

    @pytest.mark.asyncio
    async def test_send_only(self):
        mf = MockFramed()
        c = Committed(mf)
        await c.send(b'\xAA')
        assert mf.sent[0] == b'\xAA\x01'
