"""AjiHost — one TCP connection to a remote AJI server.

Sits in the component tree at ``aji/<host>[:port]`` and holds the
:class:`AjiClient` connection. Its direct children are
:class:`AjiHardware` nodes, one per cable/board the server reports
via ``get_hardware()``. Per-tap activity hangs off the
:class:`AjiHardware`, not directly off the host — see that module
for why.
"""

import logging

from .client import AjiClient

from ...db import NoMatch
from ...node import Node

from .hardware import AjiHardware, hardware_name


_logger = logging.getLogger("aji.host")


class AjiHost(Node):
    """One AJI server connection."""

    def __init__(self, name: str, *, host: str, port: int) -> None:
        super().__init__(name)
        self.__host_addr = host
        self.__port = port
        self.__client: AjiClient | None = None

    @property
    def server_address(self) -> tuple[str, int]:
        return self.__host_addr, self.__port

    @property
    def client(self) -> AjiClient | None:
        """``AjiClient`` once :meth:`start` has connected, else ``None``.
        Used by child :class:`AjiHardware` nodes to make calls."""
        return self.__client

    # --- Lifecycle ---

    async def start(self) -> None:
        """Connect, query ``get_hardware()``, and pre-attach an
        :class:`AjiHardware` child per server-side hardware. The
        children's own ``start()`` runs lazily during
        ``start_tree()`` or ``child_summon`` walks.
        """
        self.__client = await AjiClient.connect(self.__host_addr, self.__port)
        _logger.info("connected to %s:%d (server v=%d, %r)",
                     self.__host_addr, self.__port,
                     self.__client.server_version,
                     self.__client.server_version_info)

        for hw in await self.__client.get_hardware():
            name = hardware_name(hw)
            # Disambiguate duplicates by suffixing with chain_id.
            existing = {c._name for c in self._children}
            if name in existing:
                name = f"{name}-{hw.chain_id:x}"
            self.child_add(AjiHardware(name=name, hw=hw))

        from ...lifecycle import on_shutdown
        on_shutdown(self.stop)

    async def stop(self) -> None:
        from ...lifecycle import cancel_shutdown
        cancel_shutdown(self.stop)
        client = self.__client
        if client is None:
            return
        # Children's stop_tree handles their own unlock/close.
        # Just drop the connection here.
        try:
            await client.close()
        except Exception:
            pass
        self.__client = None

    # --- Path resolution ---

    async def child_spawn(self, name: str):
        # All hardwares are pre-attached in start().
        raise NoMatch("hardware", name)

    def __repr__(self) -> str:
        return (f"<AjiHost {self._name} {self.__host_addr}:{self.__port} "
                f"hardwares={len(self._children)}>")
