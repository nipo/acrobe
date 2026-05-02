"""Wire enumerator — exposes config-declared remote servers as a
local subtree under `wire/`.

Intent: `acrobe info enumerate -r wire/server0/ub3-/jtag/chain` walks
the same hw_root.child_summon machinery as a local path. The
walker decides per request whether each remote segment can be
served by REST enumeration alone or whether it crosses a
@wire.node boundary that needs WS transport.

Cutoff rule: walk REST through the requested path; the deepest
segment whose remote `wire_uuid` matches a locally-registered
@wire.node is the wire cutoff. WS opens there; subsequent
segments are walked locally on the proxy (which IS-A the
registered class — child_spawn / db / subclass methods all
work). Today only JtagInterface is @wire.node, so JTAG is the
only cutoff option; when Chain/Tap join, the deepest wins
automatically — no API change.

Configuration shape (in `~/.config/acrobe.conf`):

    wire:
      servers:
        server0:
          base: http://10.0.4.9:1234
          # user, password, token: reserved for later; OpenAuth for now.
"""

import uuid as uuid_lib
from dataclasses import dataclass
from typing import Optional

from ..configuration import Configuration, get_configuration
from ..db import NoMatch
from ..node import Node
from .client import EnumerationClient


class WireEnumerator:
    """`make_hw_root` wires this in alongside USB/AJI/XVC.

    Matches a single name `wire`; hands back a `WireNamespace`
    populated from the config's `wire.servers` section.
    """

    def __init__(self, configuration: Optional[Configuration] = None):
        self._configuration = configuration or get_configuration()

    async def spawn(self, name: str):
        if name != "wire":
            raise NoMatch("wire", name)
        return WireNamespace(self._configuration)

    async def scan(self):
        """No physical scan — listing belongs to `wire/`'s child_hints."""
        return []


class WireNamespace(Node):
    """Top of the `wire/` subtree. Children are configured servers."""

    def __init__(self, configuration: Configuration):
        super().__init__("wire")
        self._configuration = configuration

    def child_hints(self) -> list[str]:
        return list(self._servers().keys())

    async def child_spawn(self, name: str):
        servers = self._servers()
        if name not in servers:
            raise NoMatch("wire server", name)
        spec = servers[name]
        if not isinstance(spec, dict) or "base" not in spec:
            raise NoMatch("wire server", f"{name} (missing 'base' URL)")
        return RemoteServerRoot(name, spec["base"])

    def _servers(self) -> dict:
        section = self._configuration.section("wire")
        servers = section.get("servers", {})
        return servers if isinstance(servers, dict) else {}


@dataclass
class _Cutoff:
    """Deepest @wire.node found while REST-walking a requested path.

    `depth` is the index into the segment list passed to
    child_summon — segments 0..depth land on the remote @wire.node;
    segments depth+1.. are walked locally on the proxy.
    """
    depth: int
    target_class: type
    remote_path: str
    connect_url: str
    name: str


class RemoteServerRoot(Node):
    """One configured wire server. Holds an open EnumerationClient
    for the lifetime of the node (from start_tree to stop_tree).

    Children are pre-populated from a REST GET of the server's root
    on start. `child_summon` walks the requested path with
    cutoff awareness — see module docstring."""

    def __init__(self, name: str, base_url: str):
        super().__init__(name)
        self._base_url = base_url
        self._client: Optional[EnumerationClient] = None
        self._info: Optional[dict] = None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def start(self):
        self._client = EnumerationClient(self._base_url)
        await self._client.__aenter__()
        self._info = await self._client.enumerate("")
        self._metadata = self._info.get("metadata", {})
        for child_info in self._info.get("children", []):
            child = _build_remote_child(self, child_info)
            self._child_attach(child)
        # Catch the case where stop_tree() never gets called (CLI exit,
        # crash, …): the lifecycle drains us at process shutdown.
        from ..lifecycle import on_shutdown
        on_shutdown(self.stop)

    async def stop(self):
        from ..lifecycle import cancel_shutdown
        cancel_shutdown(self.stop)
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    def child_hints(self) -> list[str]:
        if self._info is None:
            return []
        return list(self._info.get("hints", []))

    async def child_summon(self, *parts):
        """Resolve `parts` against the remote server.

        Walks REST to find the deepest @wire.node along the path.
        If found, opens a WS proxy there and walks the remaining
        segments locally on it. Otherwise falls back to plain
        REST proxy traversal.
        """
        if not parts:
            return self

        cutoff = await self._find_cutoff(parts)
        if cutoff is None:
            # No transportable node in the path — fall back to the
            # standard one-segment-at-a-time walk via child_spawn,
            # producing RemoteProxyNodes for enumeration.
            return await super().child_summon(*parts)

        proxy = await self._open_proxy(cutoff)
        # Hook into the local tree under self so the proxy has a
        # parent and is reachable for stop_tree.
        if proxy._parent is None:
            self._child_attach(proxy)
        if not proxy._started:
            await proxy.start()
            proxy._started = True

        remaining = parts[cutoff.depth + 1:]
        if not remaining:
            return proxy
        return await proxy.child_summon(*remaining)

    async def _find_cutoff(self, parts) -> Optional[_Cutoff]:
        """REST-walk `parts`, return the deepest @wire.node along the
        way or None. Stops walking as soon as a step's REST GET
        fails — leaves fallback handling to the caller."""
        from . import default_registry
        registry = default_registry()
        deepest: Optional[_Cutoff] = None
        current = ""
        for depth, segment in enumerate(parts):
            current = f"{current}/{segment}".lstrip("/")
            try:
                info = await self._fetch(current)
            except Exception:
                return deepest

            wire_uuid_str = info.get("wire_uuid")
            if not wire_uuid_str:
                continue
            try:
                entry = registry.lookup_by_uuid(uuid_lib.UUID(wire_uuid_str))
            except (KeyError, ValueError):
                continue
            if entry.kind != "node":
                continue
            connect_url = info.get("connect_url")
            if not connect_url:
                continue
            deepest = _Cutoff(
                depth=depth,
                target_class=entry.cls,
                remote_path=info["path"],
                connect_url=connect_url,
                name=info["name"])
        return deepest

    async def _open_proxy(self, cutoff: _Cutoff):
        """Open a WS to the cutoff and build a local proxy that
        IS-A `cutoff.target_class`."""
        from . import default_registry
        from .client import WireClient, make_remote_proxy

        wire_client = await WireClient.connect(
            cutoff.connect_url, default_registry())
        return make_remote_proxy(
            cutoff.target_class, wire_client, name=cutoff.name)

    async def child_spawn(self, name: str):
        info = await self._fetch(name)
        return _build_remote_child(self, info)

    async def fetch(self, remote_path: str) -> dict:
        """Fetch metadata for a remote path. Used by descendant proxies
        to drill deeper without holding their own client."""
        return await self._fetch(remote_path)

    async def _fetch(self, remote_path: str) -> dict:
        if self._client is None:
            raise RuntimeError(
                f"RemoteServerRoot {self._name!r}: client not started")
        return await self._client.enumerate(remote_path)


class RemoteProxyNode(Node):
    """A node materialized from a REST enumeration response.

    Holds a back-reference to its `RemoteServerRoot` so child_spawn
    can issue further REST GETs without each proxy maintaining its
    own HTTP client.
    """

    def __init__(self, name: str, server: RemoteServerRoot,
                 remote_path: str, info: dict):
        super().__init__(name)
        self._server = server
        self._remote_path = remote_path
        self._info = info
        self._metadata = dict(info.get("metadata", {}))

    @property
    def remote_path(self) -> str:
        return self._remote_path

    @property
    def info(self) -> dict:
        """The full REST response body for this node (type, wire_uuid,
        connect_url, etc.). Read-only view."""
        return dict(self._info)

    def child_hints(self) -> list[str]:
        return list(self._info.get("hints", []))

    async def start(self):
        # Pre-populate children from the cached info so users can
        # immediately walk `.children` without an extra round trip.
        for child_info in self._info.get("children", []):
            if any(c._name == child_info["name"] for c in self._children):
                continue
            child = _build_remote_child(self._server, child_info)
            self._child_attach(child)

    async def child_spawn(self, name: str):
        child_path = (f"{self._remote_path}/{name}"
                      if self._remote_path else name)
        info = await self._server.fetch(child_path)
        return _build_remote_child(self._server, info)


def _build_remote_child(server: RemoteServerRoot, info: dict) -> RemoteProxyNode:
    return RemoteProxyNode(
        name=info["name"],
        server=server,
        remote_path=info["path"],
        info=info)
