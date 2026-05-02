"""REST enumeration handler + client integration tests.

Spins up the aiohttp Application against a synthetic Node tree
(no hardware), exercises both the raw HTTP route shape and the
EnumerationClient round trip.
"""

from dataclasses import dataclass

import pytest
from aiohttp.test_utils import TestClient, TestServer

from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.wire import Registry
from acrobe.wire.client import EnumerationClient, NodeNotFound
from acrobe.wire.server import make_app


# ----- Synthetic tree fixtures -----

class _Plain(Node):
    """Non-Batcher node — should never get a connect_url."""

    def __init__(self, name, children=()):
        super().__init__(name)
        for c in children:
            self._child_attach(c)


class _RemoteCapable(Node, Batcher):
    """Batcher node, candidate for @wire.node registration."""

    def __init__(self, name, children=()):
        Node.__init__(self, name)
        Batcher.__init__(self)
        for c in children:
            self._child_attach(c)

    async def flush_ops(self, batch):
        for _, fut in batch:
            if not fut.done():
                fut.set_result(None)


@dataclass
class _StubOp:
    value: int


def _make_registry_and_register(node_cls):
    reg = Registry()
    reg.register(_StubOp, "op", "40000000-0000-4000-8000-000000000001")
    reg.register(node_cls, "node", "40000000-0000-4000-8000-0000000000ff",
                 uses=[_StubOp])
    return reg


def _build_tree():
    """Build a small tree:

        root (_Plain)
        ├── plain-child (_Plain)
        └── remote (_RemoteCapable, registered)
            └── leaf (_Plain)
    """
    leaf = _Plain("leaf")
    remote = _RemoteCapable("remote", children=[leaf])
    plain_child = _Plain("plain-child")
    root = _Plain("root", children=[plain_child, remote])
    return root, remote


# ----- Pure handler tests via aiohttp test client -----

@pytest.mark.asyncio
async def test_root_enumeration_shape():
    root, remote = _build_tree()
    reg = _make_registry_and_register(type(remote))
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/node")
        assert resp.status == 200
        data = await resp.json()

    assert data["name"] == "root"
    assert data["type"] == "_Plain"
    assert data["is_batcher"] is False
    assert data["wire_uuid"] is None
    assert data["connect_url"] is None
    child_names = {c["name"] for c in data["children"]}
    assert child_names == {"plain-child", "remote"}


@pytest.mark.asyncio
async def test_nested_node_returns_metadata_and_children():
    root, remote = _build_tree()
    reg = _make_registry_and_register(type(remote))
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/node/remote")
        assert resp.status == 200
        data = await resp.json()

    assert data["name"] == "remote"
    assert data["type"] == "_RemoteCapable"
    assert data["is_batcher"] is True
    assert data["wire_uuid"] == "40000000-0000-4000-8000-0000000000ff"
    assert data["connect_url"] is not None
    assert data["connect_url"].startswith("ws://")
    assert "/v1/node/" in data["connect_url"]
    assert "remote" in data["connect_url"]
    assert "?token=" in data["connect_url"]
    child_names = {c["name"] for c in data["children"]}
    assert child_names == {"leaf"}


@pytest.mark.asyncio
async def test_plain_node_has_no_connect_url():
    root, remote = _build_tree()
    reg = _make_registry_and_register(type(remote))
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/node/plain-child")
        assert resp.status == 200
        data = await resp.json()

    assert data["wire_uuid"] is None
    assert data["connect_url"] is None
    assert data["is_batcher"] is False


@pytest.mark.asyncio
async def test_unknown_path_returns_404():
    root, remote = _build_tree()
    reg = _make_registry_and_register(type(remote))
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/v1/node/nope")
        assert resp.status == 404
        data = await resp.json()
        assert data["error"] == "node_not_found"


# ----- Through the wire client -----

@pytest.mark.asyncio
async def test_enumeration_client_round_trip():
    root, remote = _build_tree()
    reg = _make_registry_and_register(type(remote))
    app = make_app(root, registry=reg)

    async with TestClient(TestServer(app)) as cli:
        base = str(cli.make_url(""))
        async with EnumerationClient(base, session=cli.session) as client:
            root_data = await client.enumerate("")
            assert root_data["name"] == "root"

            remote_data = await client.enumerate("remote")
            assert remote_data["name"] == "remote"
            assert remote_data["connect_url"] is not None

            with pytest.raises(NodeNotFound):
                await client.enumerate("nope")
