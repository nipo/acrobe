"""Server-side wire entry points.

`rest` carries the REST enumeration handler and aiohttp Application
construction. `ws` (added in phase 4) carries the WebSocket upgrade
handler and per-connection batch dispatch.
"""

from .rest import (
    REST_PATH_PREFIX,
    EnumerationServer,
    make_app,
)

__all__ = [
    "REST_PATH_PREFIX",
    "EnumerationServer",
    "make_app",
]
