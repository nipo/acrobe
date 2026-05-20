"""Ft245SyncPipe: sized vs unsized reads on top of a mock transport.

The mock substitutes for FtdiTransport's ``read`` / ``read_some``
contract: exact-size for ``read(size)``, "at least one byte" for
``read_some(max_size)``, with a shared residual that holds leftovers
between calls.
"""

from __future__ import annotations

import asyncio
import pytest

from ausb.exception import TransferTimeout

from acrobe.adapter.ftdi.sync_fifo import Ft245SyncPipe


class MockFtdiTransport:
    """Models FtdiTransport's exact-size + read_some contract.

    ``push(data)`` queues bytes that subsequent reads will consume.
    ``read(size)`` blocks until exactly ``size`` bytes have arrived
    or ``timeout`` elapses (raising :class:`TransferTimeout`).
    ``read_some(max_size)`` returns the first 1..max_size bytes once
    any data is available, or raises on timeout.
    """

    def __init__(self):
        self.tx_log = bytearray()
        self.__queue: asyncio.Queue = asyncio.Queue()
        self.__residual = bytearray()
        self.__data_event = asyncio.Event()

    async def write(self, data: bytes):
        self.tx_log.extend(data)

    def push(self, data: bytes):
        self.__queue.put_nowait(bytes(data))
        self.__data_event.set()

    async def __drain_one(self, timeout: float | None):
        loop = asyncio.get_event_loop()
        if timeout is None:
            chunk = await self.__queue.get()
        else:
            remaining = timeout if timeout > 0 else 0
            try:
                chunk = await asyncio.wait_for(
                    self.__queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TransferTimeout("MockFtdiTransport: timeout")
        self.__residual.extend(chunk)

    async def read(self, size: int, timeout: float | None = 1.0) -> bytes:
        loop = asyncio.get_event_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while len(self.__residual) < size:
            remaining = None if deadline is None else deadline - loop.time()
            await self.__drain_one(remaining)
        data = bytes(self.__residual[:size])
        del self.__residual[:size]
        return data

    async def read_some(self, max_size: int,
                        timeout: float | None = 1.0) -> bytes:
        loop = asyncio.get_event_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while not self.__residual:
            remaining = None if deadline is None else deadline - loop.time()
            await self.__drain_one(remaining)
        take = min(len(self.__residual), max_size)
        data = bytes(self.__residual[:take])
        del self.__residual[:take]
        return data


@pytest.mark.asyncio
async def test_unsized_read_returns_first_available_bytes():
    t = MockFtdiTransport()
    pipe = Ft245SyncPipe(t)
    t.push(b"hello world")
    data = await pipe.read(None)
    assert data == b"hello world"


@pytest.mark.asyncio
async def test_sized_read_returns_exact_size():
    t = MockFtdiTransport()
    pipe = Ft245SyncPipe(t)
    t.push(b"HEADER_PAYLOAD")
    header = await pipe.read(6)
    assert header == b"HEADER"
    rest = await pipe.read(None)
    assert rest == b"_PAYLOAD"


@pytest.mark.asyncio
async def test_unsized_then_sized_drains_residual():
    """An unsized read takes only what was available, sized read
    drains the rest plus any new data."""
    t = MockFtdiTransport()
    pipe = Ft245SyncPipe(t)
    t.push(b"AAAA")
    first = await pipe.read(None)
    assert first == b"AAAA"
    t.push(b"BBBBBB")
    eight = await pipe.read(6)
    assert eight == b"BBBBBB"


@pytest.mark.asyncio
async def test_writes_go_through_to_transport():
    t = MockFtdiTransport()
    pipe = Ft245SyncPipe(t)
    await pipe.write(b"payload")
    await pipe.write(b" more")
    assert bytes(t.tx_log) == b"payload more"


@pytest.mark.asyncio
async def test_unsized_caps_at_max():
    """A read_some-backed unsized read returns at most
    UNSIZED_READ_MAX bytes per call."""
    t = MockFtdiTransport()
    pipe = Ft245SyncPipe(t)
    big = bytes(range(256)) * 100  # 25 600 bytes
    t.push(big)
    first = await pipe.read(None)
    assert 0 < len(first) <= Ft245SyncPipe.UNSIZED_READ_MAX
    # Remaining bytes still readable in subsequent calls.
    rest = bytearray()
    while len(rest) < len(big) - len(first):
        chunk = await pipe.read(None)
        rest.extend(chunk)
    assert first + bytes(rest) == big


@pytest.mark.asyncio
async def test_unsized_blocks_until_data_arrives():
    t = MockFtdiTransport()
    pipe = Ft245SyncPipe(t)
    read_fut = pipe.read(None)
    for _ in range(5):
        await asyncio.sleep(0)
    assert not read_fut.done()
    t.push(b"X")
    data = await asyncio.wait_for(read_fut, timeout=1.0)
    assert data == b"X"


@pytest.mark.asyncio
async def test_hdlc_over_sync_pipe_round_trip():
    """End-to-end smoke test: HDLC over Ft245SyncPipe must round-trip
    payloads now that the sync_fifo supports streaming reads."""
    from acrobe.component.nsl.bnoc.hdlc import Hdlc

    t_a, t_b = MockFtdiTransport(), MockFtdiTransport()

    # Cross-couple the two transports: write on A appears on B's
    # read queue (and vice versa).
    class CrossTransport:
        def __init__(self, tx_sink: MockFtdiTransport,
                     rx_src: MockFtdiTransport):
            self.__tx = tx_sink
            self.__rx = rx_src

        async def write(self, data):
            self.__rx.push(data)

        async def read(self, size, timeout=1.0):
            return await self.__tx.read(size, timeout=timeout)

        async def read_some(self, max_size, timeout=1.0):
            return await self.__tx.read_some(max_size, timeout=timeout)

    a_to_b = CrossTransport(t_a, t_b)
    b_to_a = CrossTransport(t_b, t_a)

    pipe_a = Ft245SyncPipe(a_to_b, name="a")
    pipe_b = Ft245SyncPipe(b_to_a, name="b")
    hdlc_a = Hdlc(pipe_a)
    hdlc_b = Hdlc(pipe_b)

    payloads = [b"first", b"second", b"third with stuffed \x7e\x7d bytes"]
    for p in payloads:
        await hdlc_a.send(p)
    received = []
    for _ in payloads:
        data, _ = await hdlc_b.recv()
        received.append(data)
    assert received == payloads
