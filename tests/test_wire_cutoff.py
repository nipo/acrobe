"""Cutoff + MRO test: deepest @wire.node along a remote path becomes
the WS boundary; segments below are walked locally on a proxy that
IS-A the registered class.

The synthetic shape mirrors the JTAG layout in spirit:
    server root → adapter (plain) → iface (@wire.node) → child (local)

`iface` is the @wire.node — `child_spawn` is defined locally on
the class. When the user summons `wire/server/adapter/iface/named-child`,
the wire layer transports `iface`, then `iface.child_spawn("named-child")`
runs locally on the proxy and produces a local child whose ops, in
turn, would route through the proxy → wire → real iface (here a
`_TestIface` Batcher).
"""

import textwrap
from dataclasses import dataclass

import pytest
from aiohttp.test_utils import TestServer

from acrobe.adapter.model import HwRoot
from acrobe.configuration import Configuration
from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe import wire
from acrobe.wire import WireEnumerator
from acrobe.wire.server import make_app


# --- Synthetic ops + nodes ---

@wire.op("70000000-0000-4000-8000-000000000001")
@dataclass
class _CutoffPing:
    nonce: int


class _LocalChild(Node):
    """Local-only Node spawned by _TestIface. Carries the iface name as
    a property so the test can assert it was constructed under the proxy
    (not via REST)."""

    def __init__(self, name, iface_name):
        super().__init__(name)
        self.iface_name = iface_name


@wire.node("70000000-0000-4000-8000-0000000000ff",
           uses=[_CutoffPing])
class _TestIface(Node, Batcher):
    """A @wire.node Batcher with a child_spawn that produces local
    children based on the requested name."""

    def __init__(self, name="iface"):
        Node.__init__(self, name)
        Batcher.__init__(self)
        self.posted = []

    async def flush_ops(self, batch):
        for op, fut in batch:
            self.posted.append(op)
            if not fut.done():
                fut.set_result(op.nonce + 1 if isinstance(op, _CutoffPing) else None)

    async def child_spawn(self, name):
        if name == "named-child":
            return _LocalChild("named-child", iface_name=self.name)
        from acrobe.db import NoMatch
        raise NoMatch("test iface child", name)


def _build_remote_tree():
    iface = _TestIface(name="iface")
    adapter = Node("adapter")
    adapter._child_attach(iface)
    root = Node("HwRoot")
    root._child_attach(adapter)
    return root


# --- Test harness ---

def _make_local_root(server_url, tmp_path):
    cfg = tmp_path / "acrobe.conf"
    cfg.write_text(textwrap.dedent(f"""
        wire:
          servers:
            srv:
              base: {server_url}
    """).strip())
    root = HwRoot()
    root.add_enumerator(WireEnumerator(configuration=Configuration(path=cfg)))
    return root


# --- Tests ---

@pytest.mark.asyncio
async def test_cutoff_lands_on_wire_node_when_path_terminates_there(tmp_path):
    """Summoning the iface itself returns a proxy IS-A _TestIface."""
    app = make_app(_build_remote_tree())
    async with TestServer(app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)
        try:
            iface = await local.child_summon("wire", "srv", "adapter", "iface")
            assert isinstance(iface, _TestIface)
            assert iface.name == "iface"
        finally:
            await local.stop_tree()


@pytest.mark.asyncio
async def test_proxy_routes_ops_over_wire(tmp_path):
    """Posting an op on the proxy round-trips through the wire."""
    remote = _build_remote_tree()
    server_iface = remote.children[0].children[0]
    app = make_app(remote)
    async with TestServer(app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)
        try:
            proxy = await local.child_summon(
                "wire", "srv", "adapter", "iface")
            result = await proxy.post(_CutoffPing(nonce=41))
            assert result == 42
            assert len(server_iface.posted) == 1
            assert server_iface.posted[0].nonce == 41
        finally:
            await local.stop_tree()


@pytest.mark.asyncio
async def test_below_cutoff_walks_locally_via_target_child_spawn(tmp_path):
    """Going one segment past the cutoff invokes target_class.child_spawn
    on the proxy — the resulting child is a local _LocalChild, not a
    RemoteProxyNode fetched via REST."""
    app = make_app(_build_remote_tree())
    async with TestServer(app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)
        try:
            child = await local.child_summon(
                "wire", "srv", "adapter", "iface", "named-child")
            assert isinstance(child, _LocalChild)
            assert child.iface_name == "iface"
            # The parent IS-A _TestIface (the proxy).
            assert isinstance(child.parent, _TestIface)
        finally:
            await local.stop_tree()


@pytest.mark.asyncio
async def test_no_wire_node_in_path_falls_back_to_rest_enumeration(tmp_path):
    """Without any @wire.node along the path, the original REST proxy
    behavior takes over (RemoteProxyNode)."""
    from acrobe.wire import RemoteProxyNode
    app = make_app(_build_remote_tree())
    async with TestServer(app) as server:
        local = _make_local_root(str(server.make_url("/")), tmp_path)
        try:
            adapter = await local.child_summon("wire", "srv", "adapter")
            assert isinstance(adapter, RemoteProxyNode)
            assert adapter.info["wire_uuid"] is None
        finally:
            await local.stop_tree()
