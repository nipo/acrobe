"""Wire enumerator — exposes config-declared remote servers as a
local subtree under `wire/`.

Intent: `acrobe info enumerate -r wire/server0/ub3-/jtag/chain` walks
the same hw_root.child_summon machinery as a local path, but the
segments below `server0` are resolved by REST against the remote
server. Each remote node is materialized as a `RemoteProxyNode`
holding the JSON metadata returned by the server.

Configuration shape (in `~/.config/acrobe.conf`):

    wire:
      servers:
        server0:
          base: http://10.0.4.9:1234
          # user, password, token: reserved for later; OpenAuth for now.

Only enumeration is exposed here. Operating on a remote @wire.node
(opening a WS, posting ops via RemoteBatcher) is a separate API on
top of `acrobe.wire.client`.
"""

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


class RemoteServerRoot(Node):
    """One configured wire server. Holds an open EnumerationClient
    for the lifetime of the node (from start_tree to stop_tree).

    Children are pre-populated from a REST GET of the server's root
    on start. `child_spawn` walks deeper paths via REST."""

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

    async def stop(self):
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    def child_hints(self) -> list[str]:
        if self._info is None:
            return []
        return list(self._info.get("hints", []))

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
