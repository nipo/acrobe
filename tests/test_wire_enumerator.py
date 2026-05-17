"""End-to-end test for the wire enumerator + config wiring.

Spins up an aiohttp wire server backed by a synthetic node tree,
points an isolated `Configuration` at that server's URL, and walks
`wire/<server>/...` through `HwRoot.child_summon` exactly as the
local CLI would.
"""

import textwrap

import pytest
from aiohttp.test_utils import TestServer

from acrobe.adapter.model import HwRoot
from acrobe.configuration import Configuration
from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.wire import WireEnumerator
from acrobe.wire.server import make_app


# --- Synthetic remote tree ---

class _RemoteThing(Node):
    """Plain Node we can drill into via REST enumeration."""

    def __init__(self, name, children=()):
        super().__init__(name)
        for c in children:
            self.child_add(c)


class _RemoteBatcher(Node, Batcher):
    """A Batcher with @wire-style metadata. Not @wire.node-decorated
    here (we don't need the WS surface for this test) — just exercises
    the is_batcher REST output."""

    def __init__(self, name):
        Node.__init__(self, name)
        Batcher.__init__(self)

    async def flush_ops(self, batch):
        for _, fut in batch:
            if not fut.done():
                fut.set_result(None)


def _build_remote_tree():
    chain = _RemoteBatcher("chain")
    jtag = _RemoteThing("jtag", children=[chain])
    adapter = _RemoteThing("ub3-trwjmby0", children=[jtag])
    return _RemoteThing("HwRoot", children=[adapter])


# --- Test setup helpers ---

@pytest.fixture
def remote_server_app():
    return make_app(_build_remote_tree())


def _make_local_root(server_url: str, tmp_path) -> HwRoot:
    cfg_path = tmp_path / "acrobe.conf"
    cfg_path.write_text(textwrap.dedent(f"""
        wire:
          servers:
            server0:
              base: {server_url}
    """).strip())
    config = Configuration(path=cfg_path)
    root = HwRoot()
    root.add_enumerator(WireEnumerator(configuration=config))
    return root


# --- Tests ---

@pytest.mark.asyncio
async def test_wire_namespace_lists_configured_servers(remote_server_app,
                                                      tmp_path):
    async with TestServer(remote_server_app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)

        wire_ns = await local.child_summon("wire")
        assert wire_ns.name == "wire"
        assert wire_ns.child_hints() == ["server0"]


@pytest.mark.asyncio
async def test_wire_walks_remote_root(remote_server_app, tmp_path):
    async with TestServer(remote_server_app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)

        srv = await local.child_summon("wire", "server0")
        # The remote root advertises its own children (the adapter).
        names = {c.name for c in srv.children}
        assert "ub3-trwjmby0" in names


@pytest.mark.asyncio
async def test_wire_drills_to_deep_remote_path(remote_server_app, tmp_path):
    async with TestServer(remote_server_app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)

        chain = await local.child_summon(
            "wire", "server0", "ub3-trwjmby0", "jtag", "chain")
        assert chain.name == "chain"
        # is_batcher comes from the REST response.
        assert chain.info["is_batcher"] is True
        assert chain.info["type"] == "_RemoteBatcher"


@pytest.mark.asyncio
async def test_wire_shortcut_name_resolves_via_remote_redirect(
        remote_server_app, tmp_path):
    """The remote server 302s shortcuts; the REST client follows them
    and the local proxy lands on the canonical node."""
    async with TestServer(remote_server_app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)

        node = await local.child_summon(
            "wire", "server0", "ub3-", "jtag")  # "ub3-" matches via substring
        assert node.name == "jtag"
        assert node.remote_path == "ub3-trwjmby0/jtag"


@pytest.mark.asyncio
async def test_wire_rejects_unknown_server(remote_server_app, tmp_path):
    from acrobe.db import NoMatch
    async with TestServer(remote_server_app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)

        with pytest.raises(NoMatch):
            await local.child_summon("wire", "no-such-server")


def test_configuration_returns_empty_section_when_file_absent(tmp_path):
    cfg = Configuration(path=tmp_path / "missing.conf")
    assert cfg.section("wire") == {}


def test_configuration_section_lookup(tmp_path):
    path = tmp_path / "acrobe.conf"
    path.write_text(textwrap.dedent("""
        wire:
          servers:
            host-a:
              base: http://a.example/
            host-b:
              base: http://b.example/
    """).strip())
    cfg = Configuration(path=path)
    section = cfg.section("wire")
    assert "servers" in section
    assert set(section["servers"].keys()) == {"host-a", "host-b"}
