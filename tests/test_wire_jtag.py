"""End-to-end: a JtagInterface hosted by aiohttp, accessed remotely
via RemoteBatcher.

Demonstrates that the wire stack works against a real-shape
@wire.node + bit-level JTAG ops + BitString return values, all over
a real WebSocket connection (in-process via aiohttp's TestServer).

Uses a synthetic `_LoopbackJtag` adapter — no hardware. The
adapter resolves Shift futures with deterministic TDO so the test
can assert on round-tripped BitString values.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from acrobe.bitstring import BitString
from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.protocol.jtag import (
    CaptureDr,
    CaptureIr,
    JtagInterface,
    Reset,
    Run,
    Shift,
    SwdToJtag,
)
from acrobe.wire import default_registry
from acrobe.wire.client import RemoteBatcher, WireClient
from acrobe.wire.server import make_app


class _LoopbackJtag(JtagInterface):
    """Synthetic JtagInterface used as the server-side endpoint.

    Records every op posted; for read shifts, returns the bitwise
    inverse of TDI as TDO so the test has a deterministic value to
    compare against.
    """

    def __init__(self):
        super().__init__(name="loopback")
        self.ops: list = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Shift) and op.read_tdo:
                inv = (1 << len(op.tdi)) - 1 - int(op.tdi)
                future.set_result(BitString(inv, len(op.tdi)))
            else:
                future.set_result(None)


@pytest.mark.asyncio
async def test_remote_jtag_shift_with_bitstring_round_trip():
    """A 32-bit Shift goes out, a 32-bit BitString comes back."""
    root = Node("root")
    iface = _LoopbackJtag()
    root._child_attach(iface)
    app = make_app(root)

    async with TestClient(TestServer(app)) as cli:
        url = str(cli.make_url("/v1/node/loopback"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            tdi = BitString(0x12345678, 32)
            tdo = await proxy.post(Shift(tdi=tdi, read_tdo=True))
            assert isinstance(tdo, BitString)
            assert len(tdo) == 32
            # The server inverted TDI, so TDO should match ~tdi (mod 32).
            expected = (1 << 32) - 1 - 0x12345678
            assert int(tdo) == expected
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_remote_jtag_full_op_set():
    """Each op type round-trips through the wire."""
    root = Node("root")
    iface = _LoopbackJtag()
    root._child_attach(iface)
    app = make_app(root)

    async with TestClient(TestServer(app)) as cli:
        url = str(cli.make_url("/v1/node/loopback"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)

            # Concurrent posts in one batch — each future awaits its own.
            f_reset    = proxy.post(Reset(count=8))
            f_capdr    = proxy.post(CaptureDr())
            f_capir    = proxy.post(CaptureIr())
            f_run      = proxy.post(Run(cycles=10))
            f_swd      = proxy.post(SwdToJtag())
            f_shift_w  = proxy.post(Shift(tdi=BitString(0xff, 8),
                                          read_tdo=False))
            f_shift_r  = proxy.post(Shift(tdi=BitString(0x55, 8),
                                          read_tdo=True))

            assert await f_reset is None
            assert await f_capdr is None
            assert await f_capir is None
            assert await f_run is None
            assert await f_swd is None
            assert await f_shift_w is None
            tdo = await f_shift_r
            assert isinstance(tdo, BitString)
            assert int(tdo) == 0xff ^ 0x55  # inverse of TDI
        finally:
            await client.close()

    # Server saw the ops in posting order.
    op_types = [type(op).__name__ for op in iface.ops]
    assert op_types == ["Reset", "CaptureDr", "CaptureIr", "Run",
                        "SwdToJtag", "Shift", "Shift"]


@pytest.mark.asyncio
async def test_remote_jtag_shift_with_arbitrary_bit_length():
    """Non-byte-aligned shifts: 13 bits in, 13 bits back."""
    root = Node("root")
    iface = _LoopbackJtag()
    root._child_attach(iface)
    app = make_app(root)

    async with TestClient(TestServer(app)) as cli:
        url = str(cli.make_url("/v1/node/loopback"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            tdi = BitString(0x1abc, 13)
            tdo = await proxy.post(Shift(tdi=tdi, read_tdo=True))
            assert len(tdo) == 13
            # ~tdi mod 13 bits
            expected = ((1 << 13) - 1) - (0x1abc & ((1 << 13) - 1))
            assert int(tdo) == expected
        finally:
            await client.close()
