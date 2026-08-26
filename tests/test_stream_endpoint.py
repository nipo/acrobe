"""stream_endpoint Datagram tested against a SOCK_SEQPACKET pair.

A Unix SEQPACKET socket preserves message boundaries the same way the
character device does -- one `read()` returns exactly one chunk -- so
the pair is a faithful stand-in for the driver. The endpoint's `start()`
still runs for real; only the `os.open` of the device path and the
geometry ioctl are redirected at the socket.
"""

import asyncio
import os
import socket
import struct
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="stream_endpoint is a Linux driver",
)

from acrobe.adapter.stream_endpoint import (
    Chunk, StreamEndpointAdapter, StreamEndpointDatagram,
    StreamEndpointEnumerator, StreamEndpointInfo,
)
from acrobe.db import NoMatch


DEVICE_PATH = "/dev/test-stream-ep"

CHUNK_MAX = 64
INFO = StreamEndpointInfo(
    stream_width=1, chunk_max=CHUNK_MAX,
    rx_hw_depth=1024, tx_hw_depth=1024,
    rx_buffer_size=65536, tx_buffer_size=65536)


@pytest.fixture
def endpoint(monkeypatch):
    """Yield (datagram, peer_socket). The datagram is not started yet."""
    peer, device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    peer.setblocking(False)
    device_fd = os.dup(device.fileno())
    device.close()

    real_open = os.open

    def fake_open(path, flags, *args):
        if path == DEVICE_PATH:
            os.set_blocking(device_fd, False)
            return device_fd
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(StreamEndpointInfo, "read", classmethod(lambda cls, fd: INFO))

    dg = StreamEndpointDatagram(DEVICE_PATH, name="datagram")
    try:
        yield dg, peer
    finally:
        peer.close()


def _peer_send(peer, payload, last):
    peer.send(Chunk.pack(payload, last))


def _peer_recv(peer, timeout=1.0):
    """Pull one chunk off the peer socket, spinning the loop so the
    endpoint's transmit task gets to run."""
    async def spin():
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                return peer.recv(Chunk.SIZE + CHUNK_MAX)
            except BlockingIOError:
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError("no chunk from endpoint")
                await asyncio.sleep(0.005)
    return spin()


# ----------------------------------------------------------------------
# Wire encoding
# ----------------------------------------------------------------------

def test_chunk_roundtrip():
    raw = Chunk.pack(b"hello", True)
    assert raw == struct.pack("=HH", 5, 1) + b"hello"
    assert Chunk.unpack(raw) == (b"hello", True)
    assert Chunk.unpack(Chunk.pack(b"x", False)) == (b"x", False)


def test_chunk_rejects_malformed():
    with pytest.raises(ValueError):
        Chunk.unpack(b"\x00\x00")                       # truncated header
    with pytest.raises(ValueError):
        Chunk.unpack(struct.pack("=HH", 4, 0) + b"ab")  # size / payload mismatch
    with pytest.raises(ValueError):
        Chunk.unpack(struct.pack("=HH", 0, 0x02))       # reserved flag


def test_get_info_ioctl_number():
    # _IOR(0xea, 0x00, struct stream_endpoint_info), 24 bytes.
    assert StreamEndpointInfo.STRUCT.size == 24
    expected = (2 << 30) | (24 << 16) | (0xEA << 8) | 0x00
    assert StreamEndpointInfo.GET_INFO == expected


# ----------------------------------------------------------------------
# Receive
# ----------------------------------------------------------------------

async def test_frame_reassembled_across_chunks(endpoint):
    dg, peer = endpoint
    await dg.start()
    try:
        _peer_send(peer, b"head", False)
        _peer_send(peer, b"tail", True)
        assert await asyncio.wait_for(dg.recv(), 1.0) == (b"headtail", None)
    finally:
        await dg.stop()


async def test_recvs_resolve_in_post_order(endpoint):
    dg, peer = endpoint
    await dg.start()
    try:
        first, second = dg.recv(), dg.recv()
        _peer_send(peer, b"one", True)
        _peer_send(peer, b"two", True)
        assert await asyncio.wait_for(first, 1.0) == (b"one", None)
        assert await asyncio.wait_for(second, 1.0) == (b"two", None)
    finally:
        await dg.stop()


async def test_frame_buffered_before_recv_is_posted(endpoint):
    dg, peer = endpoint
    await dg.start()
    try:
        _peer_send(peer, b"early", True)
        await asyncio.sleep(0.05)
        assert dg.rx_backlog == 1
        assert await asyncio.wait_for(dg.recv(), 1.0) == (b"early", None)
        assert dg.rx_backlog == 0
    finally:
        await dg.stop()


async def test_rx_high_water_stops_draining(endpoint):
    dg, peer = endpoint
    await dg.start()
    try:
        total = dg.RX_FRAME_HIGH_WATER + 20
        for i in range(total):
            _peer_send(peer, bytes([i & 0xFF]), True)
        await asyncio.sleep(0.05)
        # Reader disarmed at the mark; the rest stays in the socket so
        # backpressure reaches the driver rather than Python.
        assert dg.rx_backlog == dg.RX_FRAME_HIGH_WATER

        for i in range(20):
            assert (await asyncio.wait_for(dg.recv(), 1.0))[0] == bytes([i & 0xFF])
        await asyncio.sleep(0.05)
        # Draining re-armed the reader, which refilled to the mark.
        assert dg.rx_backlog == dg.RX_FRAME_HIGH_WATER
    finally:
        await dg.stop()


async def test_pending_recv_fails_on_stop(endpoint):
    dg, _peer = endpoint
    await dg.start()
    pending = dg.recv()
    await asyncio.sleep(0)
    await dg.stop()
    with pytest.raises(EOFError):
        await asyncio.wait_for(pending, 1.0)


# ----------------------------------------------------------------------
# Transmit
# ----------------------------------------------------------------------

async def test_send_one_chunk(endpoint):
    dg, peer = endpoint
    await dg.start()
    try:
        sent = dg.send(b"payload")
        assert Chunk.unpack(await _peer_recv(peer)) == (b"payload", True)
        await asyncio.wait_for(sent, 1.0)
    finally:
        await dg.stop()


async def test_send_splits_at_chunk_max(endpoint):
    dg, peer = endpoint
    await dg.start()
    try:
        data = bytes(range(256)) * 1  # 256 bytes over a 64-byte chunk max
        sent = dg.send(data)
        seen = bytearray()
        lasts = []
        while len(seen) < len(data):
            payload, last = Chunk.unpack(await _peer_recv(peer))
            assert len(payload) <= CHUNK_MAX
            seen += payload
            lasts.append(last)
        await asyncio.wait_for(sent, 1.0)
        assert bytes(seen) == data
        assert lasts == [False] * (len(lasts) - 1) + [True]
    finally:
        await dg.stop()


async def test_send_rejects_empty_frame(endpoint):
    dg, _peer = endpoint
    await dg.start()
    try:
        with pytest.raises(ValueError):
            await dg.send(b"")
    finally:
        await dg.stop()


async def test_short_write_resubmits_remainder(endpoint, monkeypatch):
    """A short write means the LAST flag was not applied; the chunk must
    be resubmitted from the payload offset the driver stopped at."""
    dg, peer = endpoint
    await dg.start()
    try:
        real_write = os.write
        calls = []

        def truncating_write(fd, data):
            calls.append(data)
            if len(calls) == 1:
                # Driver accepted 3 payload bytes and dropped the LAST
                # flag; nothing reaches the peer yet.
                return Chunk.SIZE + 3
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", truncating_write)
        sent = dg.send(b"abcdef")

        assert Chunk.unpack(await _peer_recv(peer)) == (b"def", True)
        await asyncio.wait_for(sent, 1.0)

        assert len(calls) == 2
        assert Chunk.unpack(calls[0]) == (b"abcdef", True)
        assert Chunk.unpack(calls[1]) == (b"def", True)
    finally:
        await dg.stop()


async def test_concurrent_sends_do_not_interleave(endpoint):
    dg, peer = endpoint
    await dg.start()
    try:
        a, b = b"A" * 100, b"B" * 100
        f1, f2 = dg.send(a), dg.send(b)
        frames, current = [], bytearray()
        while len(frames) < 2:
            payload, last = Chunk.unpack(await _peer_recv(peer))
            current += payload
            if last:
                frames.append(bytes(current))
                current.clear()
        await asyncio.wait_for(asyncio.gather(f1, f2), 1.0)
        assert frames == [a, b]
    finally:
        await dg.stop()


async def test_unsupported_op_rejected(endpoint):
    dg, _peer = endpoint
    await dg.start()
    try:
        with pytest.raises(TypeError):
            await dg.post(object())
    finally:
        await dg.stop()


# ----------------------------------------------------------------------
# Adapter / enumerator
# ----------------------------------------------------------------------

def _sysfs_tree(root, labels):
    """Build a fake `/sys/bus/platform/drivers/stream_endpoint` tree."""
    for index, label in enumerate(labels):
        device = root / f"4000{index}000.stream_ep" / "misc" / label
        device.mkdir(parents=True)
    return str(root)


def test_enumerator_labels(tmp_path):
    driver_dir = _sysfs_tree(tmp_path, ["module1", "module0", "backpanel"])
    enum = StreamEndpointEnumerator(driver_dir)
    assert enum.labels() == ["backpanel", "module0", "module1"]


def test_enumerator_empty_when_driver_absent(tmp_path):
    enum = StreamEndpointEnumerator(str(tmp_path / "nothing-here"))
    assert enum.labels() == []


async def test_enumerator_populate_is_idempotent(tmp_path):
    from acrobe.adapter.model import HwRoot

    driver_dir = _sysfs_tree(tmp_path, ["module0", "frontmodule"])
    root = HwRoot()
    enum = StreamEndpointEnumerator(driver_dir)
    await enum.populate(root)
    await enum.populate(root)

    adapters = root.children_of_class(StreamEndpointAdapter)
    assert sorted(a.name for a in adapters) == [
        "stream-frontmodule", "stream-module0"]
    assert sorted(a.ident for a in adapters) == [
        "/dev/frontmodule", "/dev/module0"]


async def test_adapter_spawns_datagram():
    adapter = StreamEndpointAdapter("stream-module0", "/dev/module0")
    assert adapter.child_hints() == ["datagram"]
    child = await adapter.child_spawn("datagram")
    assert isinstance(child, StreamEndpointDatagram)
    assert child.path == "/dev/module0"
    with pytest.raises(NoMatch):
        await adapter.child_spawn("jtag")
