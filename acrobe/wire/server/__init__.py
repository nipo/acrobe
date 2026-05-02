"""Server-side wire entry points.

`rest` is the REST enumeration handler. `ws` is the WebSocket
upgrade handler. Both share `/v1/node/<path>`; `make_app` installs
a single dispatcher that branches on the Upgrade header.
"""

from aiohttp import web

from ..registry import Registry
from .rest import (
    REST_PATH_PREFIX,
    WIRE_SERVER_KEY,
    EnumerationServer,
    handle_enumerate,
)
from .ws import handle_ws


async def _dispatch(request: web.Request):
    """One handler for `GET /v1/node/...` — dispatches to WS upgrade
    or REST based on the `Upgrade` header."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await handle_ws(request)
    return await handle_enumerate(request)


def make_app(hw_root, auth_backend=None,
             registry: Registry | None = None) -> web.Application:
    """Build the aiohttp Application carrying both REST and WS routes
    on the same URL space."""
    app = web.Application()
    server = EnumerationServer(hw_root, auth_backend=auth_backend,
                               registry=registry)
    app[WIRE_SERVER_KEY] = server
    app.router.add_get(REST_PATH_PREFIX, _dispatch)
    app.router.add_get(REST_PATH_PREFIX + "/{path:.*}", _dispatch)
    return app


__all__ = [
    "REST_PATH_PREFIX",
    "EnumerationServer",
    "make_app",
]
