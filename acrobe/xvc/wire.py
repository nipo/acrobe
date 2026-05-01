"""XVC wire-level constants and helpers.

The protocol has three NUL-free, ASCII-prefixed commands. Each
command has a fixed leading tag (e.g. ``shift:``) followed by binary
payload. There is no length-delimited framing — each command's
payload size is determined by the command itself.
"""

import struct

# Server-advertised version + max single-shift payload bytes.
# The legacy server used 4096 bytes (32768 bits). We keep the same
# number so existing Vivado / OpenOCD configs see no change.
SERVER_VERSION = b"xvcServer_v1.0"
MAX_SHIFT_BYTES = 4096
GETINFO_RESPONSE = SERVER_VERSION + b":" + str(MAX_SHIFT_BYTES).encode() + b"\n"

CMD_GETINFO = b"getinfo:"
CMD_SETTCK = b"settck:"
CMD_SHIFT = b"shift:"

# All commands fit in a small static set; the dispatcher peeks this
# many bytes to identify which one is incoming.
COMMAND_PREFIXES = (CMD_GETINFO, CMD_SETTCK, CMD_SHIFT)
MAX_PREFIX_LEN = max(len(p) for p in COMMAND_PREFIXES)


def encode_settck_response(period_ns: int) -> bytes:
    """Encode a ``settck:`` reply: 32-bit little-endian period in ns."""
    return struct.pack("<L", int(period_ns))


def decode_settck_request(payload: bytes) -> int:
    """Decode the 4-byte little-endian period (ns) following ``settck:``."""
    if len(payload) != 4:
        raise ValueError(f"settck: expected 4 bytes, got {len(payload)}")
    return struct.unpack("<L", payload)[0]


def decode_shift_header(header: bytes) -> int:
    """Decode the 4-byte bit-count header following ``shift:``."""
    if len(header) != 4:
        raise ValueError(f"shift: expected 4-byte header, got {len(header)}")
    return struct.unpack("<L", header)[0]
