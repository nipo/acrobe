from typing import Protocol


class Datagram(Protocol):
    """
    Async datagram transport.

    Provides unreliable message delivery (e.g., USB bulk with ZLP framing).
    Each send/recv is one complete message.
    """

    async def send(self, data: bytes) -> None:
        """Send a datagram."""
        ...

    async def recv(self) -> bytes:
        """Receive a datagram."""
        ...
