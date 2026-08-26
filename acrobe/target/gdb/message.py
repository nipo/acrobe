"""GDB Remote Serial Protocol packet framing.

Wire format: `$<payload>#<checksum>` where `<checksum>` is the
two-hex-digit sum of payload bytes mod 256.

Inside a payload, four bytes are reserved (`#`, `$`, `}`, `*`) and
escaped as `} <byte ^ 0x20>`. The `*` byte introduces run-length
encoded sequences in the *incoming* direction; this module
decodes them on receive but does not produce them on send. RLE
output is purely an optional bandwidth optimisation that complicates
the encoder for negligible benefit on a TCP link.

This module only deals with bytes — semantic decoding lives in
`protocol.py`.
"""

from __future__ import annotations


class Packet:
    """Static helpers for GDB RSP framing. Grouped on a class so
    related operations stay in one namespace without polluting the
    module surface."""

    ESCAPE = 0x7D     # '}'
    PACKET_START = 0x24   # '$'
    PACKET_END = 0x23     # '#'
    REPEAT = 0x2A     # '*'

    NEEDS_ESCAPE = frozenset({ESCAPE, PACKET_START, PACKET_END, REPEAT})

    @classmethod
    def unescape(cls, data: bytes) -> bytes:
        """Reverse `}<x^0x20>` escape and expand `*<n>` RLE runs.

        RLE encoding: `<byte>*<count>` means "the previous byte is
        repeated `count` extra times" where `count = encoded - 29 + 1`
        (i.e. the leading literal already counts as one).
        """
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == cls.ESCAPE and i + 1 < len(data):
                out.append(data[i + 1] ^ 0x20)
                i += 2
                continue
            if b == cls.REPEAT and i + 1 < len(data) and out:
                count = data[i + 1] - 29 + 1
                if count > 0:
                    out.extend([out[-1]] * count)
                i += 2
                continue
            out.append(b)
            i += 1
        return bytes(out)

    @classmethod
    def escape(cls, data: bytes) -> bytes:
        """Apply the `}<x^0x20>` escape to every reserved byte."""
        out = bytearray()
        for b in data:
            if b in cls.NEEDS_ESCAPE:
                out.append(cls.ESCAPE)
                out.append(b ^ 0x20)
            else:
                out.append(b)
        return bytes(out)

    @classmethod
    def parse(cls, packet: bytes) -> bytes | None:
        """Validate a `$...#xx` packet and return the unescaped
        payload. Returns None on a malformed or checksum-mismatched
        packet (caller may NACK)."""
        if len(packet) < 4 or packet[0] != cls.PACKET_START:
            return None
        try:
            hash_index = packet.index(cls.PACKET_END)
        except ValueError:
            return None
        if hash_index + 3 > len(packet):
            return None
        payload = packet[1:hash_index]
        try:
            received = int(packet[hash_index + 1:hash_index + 3], 16)
        except ValueError:
            return None
        expected = sum(payload) & 0xFF
        if received != expected:
            return None
        return cls.unescape(payload)

    @classmethod
    def frame(cls, payload: bytes) -> bytes:
        """Wrap unescaped payload into `$<escaped>#xx`."""
        encoded = cls.escape(payload)
        checksum = sum(encoded) & 0xFF
        return b"$" + encoded + b"#" + ("%02x" % checksum).encode("ascii")


# Module-level helpers for the common cases.

def frame(payload: bytes) -> bytes:
    return Packet.frame(payload)


def unframe(packet: bytes) -> bytes | None:
    return Packet.parse(packet)


def ok() -> bytes:
    return b"OK"


def error(code: int) -> bytes:
    return ("E%02x" % (code & 0xFF)).encode("ascii")


def hex_encoded(text: str) -> bytes:
    """Encode a string as hex-ASCII for `qRcmd` reply / `O` notify."""
    return text.encode("utf-8").hex().encode("ascii")
