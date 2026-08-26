"""GDB Remote Serial Protocol binding.

`Responder` (protocol.py) is generic over a `Debuggable` + optional
`Loadable`: it serves target.xml from the cores' Register set,
memory-map.xml from the Debuggable's regions, and routes
`vFlashErase/Write/Done` into the Loadable. `GdbServer` (server.py)
wraps it in an asyncio TCP server.
"""

from .message import Packet, frame, ok, error, hex_encoded, unframe
from .protocol import Responder
from .server import GdbServer

__all__ = [
    "Packet", "frame", "ok", "error", "hex_encoded", "unframe",
    "Responder", "GdbServer",
]
