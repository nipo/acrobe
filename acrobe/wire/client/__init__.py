"""Client-side wire entry points.

`rest` is the REST enumeration client. `ws` (added in phase 4) opens
WebSocket sessions and drives the proxy.
"""

from .rest import EnumerationClient, NodeNotFound

__all__ = [
    "EnumerationClient",
    "NodeNotFound",
]
