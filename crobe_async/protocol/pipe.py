from typing import Protocol


class Pipe(Protocol):
    """
    Async byte-stream transport.

    Provides reliable ordered byte delivery over a full-duplex channel
    (e.g., USB bulk endpoints, TCP socket).
    """

    async def write(self, data: bytes) -> None:
        """Write data to the transport."""
        ...

    async def read(self, size: int) -> bytes:
        """Read exactly `size` bytes from the transport."""
        ...
