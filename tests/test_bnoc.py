import asyncio
import pytest

from acrobe.protocol.datagram import Datagram, Send, Recv
from acrobe.component.nsl.bnoc.framed import JtagFramed
from acrobe.component.nsl.bnoc.routed import (
    Router, Route, Context, FramedEndpoint,
)
from acrobe.component.nsl.bnoc.committed import Committed


# -- Mock FIFO for JtagFramed tests --

class MockFifo:
    """Captures tx_words, returns canned rx frames."""

    def __init__(self, rx_frames=None):
        self.tx_words = []
        self._rx_frames = rx_frames or []

    async def exchange(self, tx_words, expect_frames=1):
        self.tx_words.extend(tx_words)
        rx = []
        for frame in self._rx_frames[:expect_frames]:
            rx.extend(JtagFramed.encode(frame))
        return rx


# -- Mock Datagram for Router/Committed/FramedEndpoint tests --

class MockDatagram(Datagram):
    """In-memory Datagram that captures sent data and returns canned responses."""

    def __init__(self, rx_frames=None, name: str = "mock"):
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


# -- JtagFramed helpers --

class TestJtagFramedHelpers:
    def test_encode_sets_last_on_final(self):
        assert JtagFramed.encode(b'\x01\x02\x03') == [0x01, 0x02, 0x103]

    def test_encode_single_byte(self):
        assert JtagFramed.encode(b'\xAB') == [0x1AB]

    def test_encode_empty(self):
        assert JtagFramed.encode(b'') == []

    def test_decode_strips_last(self):
        assert JtagFramed.decode([0x01, 0x02, 0x103]) == b'\x01\x02\x03'

    def test_roundtrip(self):
        original = b'\xDE\xAD\xBE\xEF'
        encoded = JtagFramed.encode(original)
        assert JtagFramed.decode(encoded) == original

    def test_split_frames(self):
        words = [0x01, 0x102, 0x03, 0x04, 0x105]
        frames = JtagFramed.split_frames(words)
        assert frames == [[0x01, 0x102], [0x03, 0x04, 0x105]]

    def test_split_frames_single(self):
        words = [0x01, 0x02, 0x103]
        frames = JtagFramed.split_frames(words)
        assert frames == [[0x01, 0x02, 0x103]]


class TestDatagramInheritance:
    def test_jtag_framed_is_datagram(self):
        jf = JtagFramed(MockFifo())
        assert isinstance(jf, Datagram)

    def test_route_is_datagram(self):
        router = Router(MockDatagram())
        route = router.route(0, 1)
        assert isinstance(route, Datagram)

    def test_committed_is_datagram(self):
        c = Committed(MockDatagram())
        assert isinstance(c, Datagram)

    def test_framed_endpoint_is_datagram(self):
        router = Router(MockDatagram())
        route = router.route(0, 1)
        ep = FramedEndpoint(route)
        assert isinstance(ep, Datagram)


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
        assert fifo.tx_words == JtagFramed.encode(tx_data)

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
        assert fifo.tx_words == JtagFramed.encode(b'\x55')

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
        inbound_ctx = Context(0xf, 5)
        mf = MockDatagram(rx_frames=[
            bytes([inbound_ctx.header()]) + b'\xBB',
        ])
        router = Router(mf)
        route = router.route(0xf, 5)

        route.send(b'\xAA')
        data, _ = await route.recv()

        assert data == b'\xBB'
        assert len(mf.sent) == 1
        header = mf.sent[0][0]
        assert header & 0xf == 5      # destination
        assert header >> 4 == 0xf     # source

    @pytest.mark.asyncio
    async def test_out_of_order_dispatch(self):
        inbound_a = Context(0xf, 0xa)
        inbound_b = Context(0xf, 0xb)

        mf = MockDatagram(rx_frames=[
            bytes([inbound_b.header()]) + b'\x22',
            bytes([inbound_a.header()]) + b'\x11',
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
        inbound = Context(0xf, 3)
        mf = MockDatagram(rx_frames=[
            bytes([inbound.header()]) + b'\xCC',
        ])
        router = Router(mf)
        route = router.route(0xf, 3)

        data, _ = await route.recv()
        assert data == b'\xCC'

    @pytest.mark.asyncio
    async def test_buffered_frames(self):
        inbound_a = Context(0xf, 1)
        inbound_b = Context(0xf, 2)

        mf = MockDatagram(rx_frames=[
            bytes([inbound_a.header()]) + b'\x11',
            bytes([inbound_b.header()]) + b'\x22',
        ])
        router = Router(mf)
        route_b = router.route(0xf, 2)

        data_b, _ = await route_b.recv()
        assert data_b == b'\x22'

        route_a = router.route(0xf, 1)
        data_a, _ = await route_a.recv()
        assert data_a == b'\x11'


# -- FramedEndpoint tests --

class TestFramedEndpoint:
    @pytest.mark.asyncio
    async def test_tag_correlation(self):
        inbound = Context(0xf, 1)
        mf = MockDatagram(rx_frames=[
            bytes([inbound.header(), 0x00]) + b'\xAA',
        ])
        router = Router(mf)
        route = router.route(0xf, 1)
        ep = FramedEndpoint(route)

        ep.send(b'\x55')
        data, _ = await ep.recv()
        assert data == b'\xAA'

    @pytest.mark.asyncio
    async def test_tag_increment(self):
        inbound = Context(0xf, 1)
        mf = MockDatagram(rx_frames=[
            bytes([inbound.header(), 0x00]) + b'\xAA',
            bytes([inbound.header(), 0x01]) + b'\xBB',
        ])
        router = Router(mf)
        route = router.route(0xf, 1)
        ep = FramedEndpoint(route)

        ep.send(b'\x11')
        r1, _ = await ep.recv()
        ep.send(b'\x22')
        r2, _ = await ep.recv()
        assert r1 == b'\xAA'
        assert r2 == b'\xBB'

    @pytest.mark.asyncio
    async def test_tag_mismatch_raises(self):
        inbound = Context(0xf, 1)
        mf = MockDatagram(rx_frames=[
            bytes([inbound.header(), 0xFF]) + b'\xAA',
        ])
        router = Router(mf)
        route = router.route(0xf, 1)
        ep = FramedEndpoint(route)

        ep.send(b'\x55')
        with pytest.raises(RuntimeError, match="tag mismatch"):
            await ep.recv()


# -- Committed tests --

class TestCommitted:
    @pytest.mark.asyncio
    async def test_commit_byte_appended_and_stripped(self):
        mf = MockDatagram(rx_frames=[b'\xAA' + bytes([Committed.COMMIT])])
        c = Committed(mf)

        c.send(b'\x01\x02')
        data, _ = await c.recv()

        assert data == b'\xAA'
        assert mf.sent[0] == b'\x01\x02' + bytes([Committed.COMMIT])

    @pytest.mark.asyncio
    async def test_send_only(self):
        mf = MockDatagram()
        c = Committed(mf)
        await c.send(b'\xAA')
        assert mf.sent[0] == b'\xAA' + bytes([Committed.COMMIT])

    @pytest.mark.asyncio
    async def test_cancelled_frame_dropped(self):
        # First arriving frame has the cancel trailer; second is a real
        # response. Committed should warn and drop the cancelled one.
        mf = MockDatagram(rx_frames=[
            b'\x99' + bytes([Committed.CANCEL]),
            b'\xAA' + bytes([Committed.COMMIT]),
        ])
        c = Committed(mf)

        c.send(b'\x01')
        data, _ = await c.recv()
        assert data == b'\xAA'
