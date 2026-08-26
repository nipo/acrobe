"""Client-side wire entry points.

`rest` is the REST enumeration client. `ws` opens WebSocket sessions
and exposes a low-level `WireClient`. `proxy` wraps a `WireClient`
in a `RemoteBatcher` that mimics the local Batcher API.
"""

from .proxy import RemoteBatcher, make_remote_proxy
from .rest import EnumerationClient, NodeNotFound
from .ws import WireClient, WireClientError

__all__ = [
    "EnumerationClient",
    "NodeNotFound",
    "RemoteBatcher",
    "WireClient",
    "WireClientError",
    "make_remote_proxy",
]
