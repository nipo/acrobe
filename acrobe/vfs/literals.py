"""Synthesised literal Nodes.

Literal Nodes generate bytes from parameters (no file source).
Useful for testing, padding, fill patterns, and for the CLI to
inject synthesised content into operations.

Available formats:
- literal(value=DEADBEEF)        -> bytes.fromhex(value)
- random(size=N)                 -> os.urandom(N)
- zero(size=N)                   -> b"\\x00" * N
- one(size=N)                    -> b"\\xff" * N

These are spawnable via the format_db using AsNode with no
source (the literal class accepts source=None). Typical use:
    `as(type=zero,size=0x400)` somewhere in a path produces
    a 1024-byte zero blob.

When invoked from `as`, AsNode calls populate_format with the
literal class. The literal's start() ignores `source` and
synthesises bytes per its options.
"""

import os

from ..node import Node, Readable
from . import FormatNode, register_format


class _LiteralBytes(Node, Readable):
    """A leaf Readable holding a fixed in-memory byte payload."""

    def __init__(self, name, data: bytes):
        super().__init__(name)
        self.__data = data

    @property
    def size(self) -> int:
        return len(self.__data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self.__data):
            raise ValueError(f"offset {offset} out of range")
        avail = len(self.__data) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return self.__data[offset:offset + n]


@register_format("literal")
class Literal(FormatNode):
    """Hex-string literal. Path: as(type=literal,value=DEADBEEF)."""

    def __init__(self, name, source):
        super().__init__(name, source)
        self.__hex_value = None

    def option_set(self, key, value):
        if key == "value":
            self.__hex_value = value
            return
        super().option_set(key, value)

    async def start(self):
        if self.__hex_value is None:
            raise ValueError(
                f"{self.fqdn}: literal requires value=...")
        data = bytes.fromhex(self.__hex_value)
        self.child_add(_LiteralBytes("data", data))


def _parse_size(text):
    """Parse size expressed as decimal or hex literal (e.g. 1024, 0x400)."""
    return int(text, 0)


@register_format("random")
class Random(FormatNode):
    """Random bytes. Path: as(type=random,size=N)."""

    def __init__(self, name, source):
        super().__init__(name, source)
        self.__size_str = None

    def option_set(self, key, value):
        if key == "size":
            self.__size_str = value
            return
        super().option_set(key, value)

    async def start(self):
        if self.__size_str is None:
            raise ValueError(
                f"{self.fqdn}: random requires size=...")
        n = _parse_size(self.__size_str)
        self.child_add(_LiteralBytes("data", os.urandom(n)))


@register_format("zero")
class Zero(FormatNode):
    """Zero bytes. Path: as(type=zero,size=N)."""

    def __init__(self, name, source):
        super().__init__(name, source)
        self.__size_str = None

    def option_set(self, key, value):
        if key == "size":
            self.__size_str = value
            return
        super().option_set(key, value)

    async def start(self):
        if self.__size_str is None:
            raise ValueError(
                f"{self.fqdn}: zero requires size=...")
        n = _parse_size(self.__size_str)
        self.child_add(_LiteralBytes("data", b"\x00" * n))


@register_format("one")
class One(FormatNode):
    """All-ones bytes (0xFF). Path: as(type=one,size=N)."""

    def __init__(self, name, source):
        super().__init__(name, source)
        self.__size_str = None

    def option_set(self, key, value):
        if key == "size":
            self.__size_str = value
            return
        super().option_set(key, value)

    async def start(self):
        if self.__size_str is None:
            raise ValueError(
                f"{self.fqdn}: one requires size=...")
        n = _parse_size(self.__size_str)
        self.child_add(_LiteralBytes("data", b"\xff" * n))
