"""XvcBroker — the ``xvc`` directory in the component tree.

Sits at ``xvc`` under HwRoot. ``child_spawn(name)`` parses the next
path component as ``<host>`` or ``<host>:<port>`` (default port
:data:`.client.DEFAULT_PORT`) and returns a fresh :class:`XvcClient`.
It owns no state of its own.
"""

import re

from ...db import NoMatch
from ...node import Node
from .client import XvcClient, DEFAULT_PORT


_HOST_PORT_RE = re.compile(
    r"^(?P<host>"
    r"\[[0-9A-Fa-f:]+\]"               # IPv6 in brackets
    r"|[A-Za-z0-9.\-]+)"               # hostname or IPv4
    r"(?::(?P<port>\d+))?$")


def _parse_host_port(name: str) -> tuple[str, int]:
    m = _HOST_PORT_RE.match(name)
    if not m:
        raise ValueError(f"not a host[:port]: {name!r}")
    host = m.group("host")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    port = int(m.group("port")) if m.group("port") else DEFAULT_PORT
    return host, port


class XvcBroker(Node):
    def __init__(self, name: str = "xvc") -> None:
        super().__init__(name)

    async def child_spawn(self, name: str) -> XvcClient:
        try:
            host, port = _parse_host_port(name)
        except ValueError:
            raise NoMatch("xvc-host", name)
        return XvcClient(name=name, host=host, port=port)
