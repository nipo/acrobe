"""REST enumeration handler.

URL space: `GET /v1/node/<path>` returns metadata + child list for the
node at `<path>`. Empty path = root.

Response shape (JSON):

    {
      "path":       "ub3-/jtag/chain",
      "name":       "chain",
      "type":       "Chain",
      "metadata":   { ... },          # node._metadata if present
      "is_batcher": true,
      "wire_uuid":  "A8F082D4-..."    # null if not a @wire.node
      "connect_url": "ws://host:port/v1/node/ub3-/jtag/chain?token=..."
                                      # null when not a @wire.node
      "children":   [
        { "path": "...", "name": "...", "type": "...",
          "is_batcher": false, "wire_uuid": null },
        ...
      ]
    }

Children are summarized (no recursion); the client follows individual
child URLs to drill in.

Probe serialization: a single asyncio.Lock around `child_summon` +
`start_tree` keeps concurrent enumerations from racing on hardware
probing. Coarse but correct; refine to per-adapter locks once a
real workload demands it.
"""

import asyncio
from urllib.parse import quote

from aiohttp import web

from ...engine import Batcher
from ...node import Node
from ..auth import OpenAuthBackend
from ..principal import Scope
from ..registry import Registry, default_registry


REST_PATH_PREFIX = "/v1/node"

WIRE_SERVER_KEY: web.AppKey["EnumerationServer"] = web.AppKey(
    "wire_server", object)


class EnumerationServer:
    """Stateful holder for the REST app's dependencies.

    Constructed once per server process. Holds the hw_root, auth
    backend, registry, and the probe-serialization lock.
    """

    def __init__(self, hw_root: Node, auth_backend=None,
                 registry: Registry | None = None):
        self.hw_root = hw_root
        self.auth = auth_backend or OpenAuthBackend()
        self.registry = registry or default_registry()
        self._probe_lock = asyncio.Lock()

    async def resolve_node(self, parts: list[str]) -> Node:
        """Walk hw_root → ... → leaf, starting nodes as needed.

        Held under the probe lock so concurrent callers don't race
        on hardware-touching start() side effects.
        """
        async with self._probe_lock:
            node = await self.hw_root.child_summon(*parts)
            if isinstance(node, Node):
                await node.start_tree()
            return node

    def canonical_path(self, node: Node) -> str:
        """Path of `node` relative to hw_root, matching the URL space.

        `Node.path` carries the hw_root's name as a leading segment;
        URLs do not. Strip it so REST `path` fields are walkable
        programmatically by appending to the URL prefix.
        """
        full = node.path
        root_name = self.hw_root.name
        if full == root_name:
            return ""
        prefix = root_name + "/"
        if full.startswith(prefix):
            return full[len(prefix):]
        return full  # node not under hw_root — leave as-is, surface oddly

    def describe(self, node: Node, request: web.Request,
                 *, include_children: bool = True) -> dict:
        """Render `node` as the REST JSON response body."""
        entry = self.registry.try_lookup_by_class(type(node))
        is_node_kind = entry is not None and entry.kind == "node"

        out = {
            "path":        self.canonical_path(node),
            "name":        node.name,
            "type":        type(node).__name__,
            "metadata":    dict(node.metadata) if hasattr(node, "metadata") else {},
            "is_batcher":  isinstance(node, Batcher),
            "wire_uuid":   str(entry.type_uuid) if entry else None,
            "connect_url": self._connect_url(node, request) if is_node_kind else None,
        }
        if include_children:
            out["children"] = [
                self.describe(c, request, include_children=False)
                for c in node.children
            ]
            out["hints"] = list(node.child_hints())
        return out

    def _connect_url(self, node: Node, request: web.Request) -> str:
        """Build the WS connect URL for a registered @wire.node.

        Token is whatever the AuthBackend issues; OpenAuthBackend
        returns the empty string. Scheme follows the inbound request
        (http→ws, https→wss).
        """
        from ..principal import Principal

        principal = self.auth.extract_principal(request)
        scope = Scope.all()
        token = self.auth.issue_connect_token(principal, node, scope)

        scheme = "wss" if request.scheme == "https" else "ws"
        host = request.host
        canonical = self.canonical_path(node)
        path = REST_PATH_PREFIX
        if canonical:
            path = path + "/" + _encode_path(canonical)
        return f"{scheme}://{host}{path}?token={quote(token, safe='')}"


def _encode_path(path: str) -> str:
    """Percent-encode each path segment, keeping the slashes."""
    return "/".join(quote(seg, safe="") for seg in path.split("/"))


async def _enumerate_handler(request: web.Request) -> web.Response:
    server: EnumerationServer = request.app[WIRE_SERVER_KEY]

    raw_path = request.match_info.get("path", "")
    requested = "/".join(p for p in raw_path.strip("/").split("/") if p)
    parts = [p for p in requested.split("/") if p]

    try:
        node = await server.resolve_node(parts)
    except Exception as exc:
        return web.json_response(
            {"error": "node_not_found",
             "path": requested,
             "detail": str(exc)},
            status=404)

    # If the resolved node's canonical path differs from the requested
    # one, the caller used a shortcut (substring/case-insensitive name).
    # Redirect to the canonical URL so clients can keep stable refs.
    canonical = server.canonical_path(node)
    if canonical != requested:
        location = REST_PATH_PREFIX
        if canonical:
            location = location + "/" + _encode_path(canonical)
        raise web.HTTPFound(location=location)

    return web.json_response(server.describe(node, request))


def make_app(hw_root: Node, auth_backend=None,
             registry: Registry | None = None) -> web.Application:
    """Build an aiohttp Application carrying the REST enumeration route.

    The same Application will gain the WS upgrade route in phase 4 —
    one app, one server, two route families.
    """
    app = web.Application()
    server = EnumerationServer(hw_root, auth_backend=auth_backend,
                               registry=registry)
    app[WIRE_SERVER_KEY] = server
    # Match the bare prefix and any sub-path including empty ("/v1/node",
    # "/v1/node/", "/v1/node/foo/bar").
    app.router.add_get(REST_PATH_PREFIX, _enumerate_handler)
    app.router.add_get(REST_PATH_PREFIX + "/{path:.*}", _enumerate_handler)
    return app
