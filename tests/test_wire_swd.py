"""End-to-end: an SWD Interface hosted by aiohttp, accessed remotely
via RemoteBatcher.

Mirrors test_wire_jtag.py but for the bit-level SWD ops and the
SwdAccessFailure / SwdWait errors. Uses a synthetic _LoopbackSwd
backend — no hardware. Read futures resolve to a deterministic
function of the address so the test can assert on round-tripped
ints; selected ops raise to verify error wire transport.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from acrobe.node import Node
from acrobe.protocol.swd import (
    Interface,
    JtagToSwd,
    LineReset,
    Read,
    Run,
    SwdAccessFailure,
    SwdWait,
    Wakeup,
    Write,
)
from acrobe.wire import default_registry
from acrobe.wire.client import RemoteBatcher, WireClient
from acrobe.wire.server import make_app


class _LoopbackSwd(Interface):
    """Synthetic SWD Interface used as the server-side endpoint.

    Records every op posted; for Reads, returns a deterministic value
    derived from the address so the test has something to compare
    against. Optional `fault_addr` / `wait_addr` route reads on those
    addresses to error futures, exercising error wire transport.
    """

    def __init__(self, fault_addr=None, wait_addr=None):
        super().__init__(name="loopback")
        self.ops: list = []
        self._fault_addr = fault_addr
        self._wait_addr = wait_addr

    async def start(self):
        """Bypass the base wire init + DP spawn — this test only
        exercises wire transport of explicitly-posted ops."""

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, Read):
                if op.addr == self._fault_addr:
                    future.set_exception(
                        SwdAccessFailure(f"FAULT on read 0x{op.addr:02x}"))
                elif op.addr == self._wait_addr:
                    future.set_exception(
                        SwdWait(f"WAIT on read 0x{op.addr:02x}"))
                else:
                    # Distinct values per (ap, addr) pair.
                    future.set_result(
                        (0xa5a5_0000 | (int(op.ap) << 8) | (op.addr & 0xff)))
            else:
                future.set_result(None)


@pytest.mark.asyncio
async def test_remote_swd_read_round_trip():
    """A Read goes out, the int comes back."""
    root = Node("root")
    iface = _LoopbackSwd()
    root.child_add(iface)
    app = make_app(root)

    async with TestClient(TestServer(app)) as cli:
        url = str(cli.make_url("/v1/node/loopback"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            value = await proxy.post(Read(ap=False, addr=0x04))
            assert value == 0xa5a5_0004
            value = await proxy.post(Read(ap=True, addr=0x0c))
            assert value == 0xa5a5_010c
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_remote_swd_full_op_set():
    """Each op type round-trips through the wire."""
    root = Node("root")
    iface = _LoopbackSwd()
    root.child_add(iface)
    app = make_app(root)

    async with TestClient(TestServer(app)) as cli:
        url = str(cli.make_url("/v1/node/loopback"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)

            f_lr     = proxy.post(LineReset())
            f_j2s    = proxy.post(JtagToSwd())
            f_wake   = proxy.post(Wakeup(cycles=64))
            f_run    = proxy.post(Run(cycles=8))
            f_write  = proxy.post(Write(ap=False, addr=0x08,
                                        data=0xdeadbeef))
            f_read   = proxy.post(Read(ap=True, addr=0x10))

            assert await f_lr is None
            assert await f_j2s is None
            assert await f_wake is None
            assert await f_run is None
            assert await f_write is None
            assert await f_read == 0xa5a5_0110
        finally:
            await client.close()

    op_types = [type(op).__name__ for op in iface.ops]
    assert op_types == ["LineReset", "JtagToSwd", "Wakeup", "Run",
                        "Write", "Read"]


@pytest.mark.asyncio
async def test_remote_swd_error_round_trip():
    """SwdAccessFailure / SwdWait raised server-side land on the client
    as the matching exception classes with the original detail string.
    """
    root = Node("root")
    iface = _LoopbackSwd(fault_addr=0x20, wait_addr=0x24)
    root.child_add(iface)
    app = make_app(root)

    async with TestClient(TestServer(app)) as cli:
        url = str(cli.make_url("/v1/node/loopback"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)

            with pytest.raises(SwdAccessFailure) as exc_info:
                await proxy.post(Read(ap=False, addr=0x20))
            # Plain SwdAccessFailure (not the SwdWait subclass).
            assert type(exc_info.value) is SwdAccessFailure
            assert "FAULT on read 0x20" in str(exc_info.value)

            with pytest.raises(SwdWait) as exc_info:
                await proxy.post(Read(ap=False, addr=0x24))
            assert "WAIT on read 0x24" in str(exc_info.value)

            # Surrounding successful reads still work.
            assert await proxy.post(Read(ap=False, addr=0x00)) \
                == 0xa5a5_0000
        finally:
            await client.close()
