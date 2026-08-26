"""HDLC framing over a Pipe: byte stuffing, FCS, header wrapping."""

import asyncio
import pytest

from acrobe.protocol.pipe import Pipe, Read, Write
from acrobe.component.hdlc import Hdlc, HdlcHeader, HdlcAddr


class LoopbackPipe(Pipe):
    """Bidirectional in-memory byte pipe with size=None support.

    Two ends share two queues. ``read(size)`` blocks for exactly
    ``size`` bytes; ``read(None)`` returns as soon as at least one
    byte is available, draining whatever has accumulated.
    """

    def __init__(self, rx: asyncio.Queue, tx: asyncio.Queue,
                 name: str = "loopback"):
        super().__init__(name)
        self.__rx = rx
        self.__tx = tx

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Write):
                for b in op.data:
                    await self.__tx.put(b)
                if not future.done():
                    future.set_result(None)
            elif isinstance(op, Read):
                asyncio.create_task(self.__read_task(op.size, future))
            else:
                if not future.done():
                    future.set_exception(TypeError(
                        f"LoopbackPipe: unsupported op {type(op).__name__}"))

    async def __read_task(self, size, future):
        try:
            out = bytearray()
            if size is None:
                out.append(await self.__rx.get())
                while not self.__rx.empty():
                    out.append(self.__rx.get_nowait())
            else:
                for _ in range(size):
                    out.append(await self.__rx.get())
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            return
        if not future.done():
            future.set_result(bytes(out))

    @classmethod
    def make_pair(cls):
        q1: asyncio.Queue = asyncio.Queue()
        q2: asyncio.Queue = asyncio.Queue()
        return cls(q1, q2), cls(q2, q1)


@pytest.mark.asyncio
async def test_packetize_round_trip_single_frame():
    a, b = LoopbackPipe.make_pair()
    tx = Hdlc(a)
    rx = Hdlc(b)
    payload = b"hello, world"
    await tx.send(payload)
    data, ctx = await rx.recv()
    assert data == payload
    assert ctx is None


@pytest.mark.asyncio
async def test_byte_stuffing_round_trip():
    """Every byte that needs escaping survives a round trip."""
    a, b = LoopbackPipe.make_pair()
    tx = Hdlc(a)
    rx = Hdlc(b)
    payload = bytes([0x00, 0x7E, 0x7D, 0x03, 0x11, 0x13, 0x91, 0x93, 0xFF])
    await tx.send(payload)
    data, _ = await rx.recv()
    assert data == payload


@pytest.mark.asyncio
async def test_wire_bytes_have_no_inband_specials():
    """The encoded wire form must not contain any escape-list byte
    outside of the leading and trailing flag and the escape marker
    sequences themselves."""
    encoded = Hdlc.packetize(bytes(range(0, 64)))
    assert encoded[0] == Hdlc.FLAG
    assert encoded[-1] == Hdlc.FLAG
    body = encoded[1:-1]
    i = 0
    while i < len(body):
        b = body[i]
        if b == Hdlc.ESCAPE:
            assert i + 1 < len(body), "trailing escape with no follower"
            i += 2
            continue
        assert b not in Hdlc.ESCAPED_BYTES, \
            f"byte {b:#04x} at offset {i} should have been escaped"
        i += 1


@pytest.mark.asyncio
async def test_multiple_frames_back_to_back():
    a, b = LoopbackPipe.make_pair()
    tx = Hdlc(a)
    rx = Hdlc(b)
    payloads = [b"one", b"two", b"three", b"four"]
    for p in payloads:
        await tx.send(p)
    received = []
    for _ in payloads:
        data, _ = await rx.recv()
        received.append(data)
    assert received == payloads


@pytest.mark.asyncio
async def test_bad_fcs_drops_frame(caplog):
    """A frame with a corrupted FCS is silently dropped (WARNING log);
    the next valid frame is delivered to the pending recv()."""
    a, b = LoopbackPipe.make_pair()
    rx = Hdlc(b)

    good = Hdlc.packetize(b"good frame")
    bad = bytearray(Hdlc.packetize(b"corrupted"))
    # Flip a byte inside the payload (avoid flag/escape positions).
    bad[3] ^= 0x55
    await a.write(bytes(bad) + good)

    import logging
    with caplog.at_level(logging.WARNING):
        data, _ = await rx.recv()
    assert data == b"good frame"
    assert any("bad FCS" in r.message for r in caplog.records), \
        f"expected a bad-FCS WARNING, got {caplog.records}"


@pytest.mark.asyncio
async def test_garbage_before_first_flag_is_ignored():
    a, b = LoopbackPipe.make_pair()
    rx = Hdlc(b)
    payload = b"after the garbage"
    await a.write(b"\x00\x01\x02junk" + Hdlc.packetize(payload))
    data, _ = await rx.recv()
    assert data == payload


@pytest.mark.asyncio
async def test_consecutive_flags_yield_no_empty_frame():
    a, b = LoopbackPipe.make_pair()
    rx = Hdlc(b)
    payload = b"the only one"
    await a.write(bytes([Hdlc.FLAG] * 5) + Hdlc.packetize(payload))
    data, _ = await rx.recv()
    assert data == payload


@pytest.mark.asyncio
async def test_chunked_arrival_reassembles_frame():
    """A frame split across multiple pipe reads must reassemble
    correctly — the unframer keeps state across feed() calls."""
    a, b = LoopbackPipe.make_pair()
    rx = Hdlc(b)
    payload = bytes(range(0, 128))
    wire = Hdlc.packetize(payload)
    for i in range(0, len(wire), 7):
        await a.write(wire[i:i + 7])
        await asyncio.sleep(0)
    data, _ = await rx.recv()
    assert data == payload


@pytest.mark.asyncio
async def test_hdlc_header_default_round_trip():
    a, b = LoopbackPipe.make_pair()
    tx = HdlcHeader(Hdlc(a))
    rx = HdlcHeader(Hdlc(b))
    payload = b"with default header"
    await tx.send(payload)
    data, hdr = await rx.recv()
    assert data == payload
    assert hdr == HdlcAddr(0x00, 0x13)


@pytest.mark.asyncio
async def test_hdlc_header_custom_context_round_trip():
    a, b = LoopbackPipe.make_pair()
    tx = HdlcHeader(Hdlc(a))
    rx = HdlcHeader(Hdlc(b))
    payload = b"with custom header"
    await tx.send(payload, context=HdlcAddr(0x42, 0x55))
    data, hdr = await rx.recv()
    assert data == payload
    assert hdr == HdlcAddr(0x42, 0x55)


@pytest.mark.asyncio
async def test_hdlc_header_undersized_frame_raises():
    """If the lower datagram delivers a frame shorter than 2 bytes,
    HdlcHeader must surface that as an error rather than silently
    masking it."""
    a, b = LoopbackPipe.make_pair()
    rx = HdlcHeader(Hdlc(b))
    # Send a 1-byte payload through raw Hdlc; HdlcHeader will see
    # a frame too short to carry an address+control header.
    await a.write(Hdlc.packetize(b"\xaa"))
    with pytest.raises(RuntimeError, match="undersized"):
        await rx.recv()
