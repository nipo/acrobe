"""End-to-end WebSocket transport test.

Spins up a real aiohttp app on a real socket via TestServer,
connects with WireClient, drives a synthetic EchoNode through
RemoteBatcher.

Covers: results round trip, declared errors, InternalError
wrapping, multiple in-flight batches resolved out of order.
"""

from dataclasses import dataclass

import pytest
from aiohttp.test_utils import TestClient, TestServer

from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.wire import (
    InternalError,
    Registry,
)
from acrobe.wire.client import RemoteBatcher, WireClient
from acrobe.wire.server import make_app


# ----- Test types -----

@dataclass
class _WsEcho:
    value: int


@dataclass
class _WsShout:
    text: str
    times: int = 1


@dataclass
class _WsBoom(Exception):
    reason: str


@dataclass
class _WsRaiseSurprise:
    note: str


class _WsSurpriseException(Exception):
    """Not registered — must be wrapped by InternalError on transit."""


class _WsEchoNode(Node, Batcher):
    """Server-side node executing the test ops."""

    def __init__(self):
        Node.__init__(self, "echo")
        Batcher.__init__(self)

    async def flush_ops(self, batch):
        for op, fut in batch:
            if fut.done():
                continue
            if isinstance(op, _WsEcho):
                fut.set_result(op.value * 2)
            elif isinstance(op, _WsShout):
                fut.set_result(op.text.upper() + ("!" * op.times))
            elif isinstance(op, _WsBoom):
                fut.set_exception(op)
            elif isinstance(op, _WsRaiseSurprise):
                fut.set_exception(_WsSurpriseException(op.note))
            else:
                fut.set_exception(
                    RuntimeError(f"unknown op {type(op).__name__}"))


def _make_registry():
    reg = Registry()
    reg.register(_WsEcho, "op",
                 "50000000-0000-4000-8000-000000000001")
    reg.register(_WsShout, "op",
                 "50000000-0000-4000-8000-000000000002")
    reg.register(_WsRaiseSurprise, "op",
                 "50000000-0000-4000-8000-000000000003")
    reg.register(_WsBoom, "error",
                 "50000000-0000-4000-8000-0000000000fe")
    # InternalError comes from the default registry — register it
    # against this isolated one for the dispatcher's wrapping path
    # and the client's decode path.
    reg.register(InternalError, "error",
                 str(InternalError.__wire_uuid__))
    reg.register(_WsEchoNode, "node",
                 "50000000-0000-4000-8000-0000000000ff",
                 uses=[_WsEcho, _WsShout, _WsRaiseSurprise,
                       _WsBoom, InternalError])
    return reg


def _make_root_with_node():
    """A non-Batcher root containing the echo node as 'echo'."""
    root = Node("root")
    echo = _WsEchoNode()
    root._child_attach(echo)
    return root, echo


# ----- WS handshake & dispatch -----

@pytest.mark.asyncio
async def test_ws_handshake_and_simple_batch():
    reg = _make_registry()
    root, _ = _make_root_with_node()
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        ws_url = cli.make_url("/v1/node/echo")
        # Use TestClient's session so the WS upgrade goes through the
        # in-process server, not a real network hop.
        client = await WireClient.connect(str(ws_url), reg,
                                          http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            result = await proxy.post(_WsEcho(value=21))
            assert result == 42

            shout_result = await proxy.post(_WsShout(text="hi", times=3))
            assert shout_result == "HI!!!"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_declared_error_round_trip():
    reg = _make_registry()
    root, _ = _make_root_with_node()
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        client = await WireClient.connect(
            str(cli.make_url("/v1/node/echo")), reg,
            http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            with pytest.raises(_WsBoom) as excinfo:
                await proxy.post(_WsBoom(reason="kaboom"))
            assert excinfo.value.reason == "kaboom"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_unhandled_exception_wraps_as_internal_error():
    reg = _make_registry()
    root, _ = _make_root_with_node()
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        client = await WireClient.connect(
            str(cli.make_url("/v1/node/echo")), reg,
            http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            with pytest.raises(InternalError) as excinfo:
                await proxy.post(_WsRaiseSurprise(note="oops"))
            assert "Surprise" in excinfo.value.representation
            assert "oops" in excinfo.value.representation
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_concurrent_batches():
    """Two batches in flight at once — req_ids correlate, ordering on
    the wire is independent of completion order on the server."""
    import asyncio

    reg = _make_registry()
    root, _ = _make_root_with_node()
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        client = await WireClient.connect(
            str(cli.make_url("/v1/node/echo")), reg,
            http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            f1 = proxy.post(_WsEcho(value=1))
            f2 = proxy.post(_WsEcho(value=2))
            f3 = proxy.post(_WsEcho(value=100))
            results = await asyncio.gather(f1, f2, f3)
            assert results == [2, 4, 200]
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_rejects_non_node_path():
    """Opening WS to a path that isn't a registered @wire.node fails."""
    reg = _make_registry()
    root, _ = _make_root_with_node()
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        # `root` is a plain Node, not @wire.node-registered.
        from aiohttp import WSServerHandshakeError
        with pytest.raises(WSServerHandshakeError):
            await cli.session.ws_connect(str(cli.make_url("/v1/node")))