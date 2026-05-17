"""Libaji-faithful AJI wire layer.

Direct port of the encoding/decoding logic from
``libaji_client/src/jtag/jtag_message.h`` and
``libaji_client/src/jtag/jtag_tcplink.cpp``. The names, opcodes and
byte layout match libaji byte-for-byte; this is the reference
implementation we use to talk to real ``jtagd`` instances.

Mux header (libaji)
-------------------
Each TCP packet is prefixed with a 2-byte big-endian header:

    bits 15..12: mux channel (0 = command, 4..15 = FIFO 0..11)
    bits 11..0:  payload length minus one  (so 1..4096 bytes payload)

Block header (inside command/response payload)
----------------------------------------------
Each command/response block is prefixed with 4 bytes:

    byte 0: command code (or response status)
    byte 1: reserved (must be 0)
    bytes 2..3: total block length including this 4-byte header
                (big-endian, max 0xFFFF)

Multiple blocks can be concatenated within a single mux packet.

Greeting (server -> client, single block on mux 0)
--------------------------------------------------
The greeting block is a regular MESSAGE block whose payload is:

    string  AJI_SIGNATURE  ("JTAG Server\\r\\n", length-prefixed)
    int     server_version
    int     authtype       (0 or AUTHENTICATE_MD5)
    raw[16] challenge      (only if authtype == AUTHENTICATE_MD5)

The libaji client sends back AUTHENTICATE_MD5 if challenged, then
``USE_PROTOCOL_VERSION + GET_VERSION_INFO``.
"""

import enum
import struct
from typing import Self


# --- Constants -------------------------------------------------------------

#: Latest protocol version supported by this implementation. Mirrors
#: ``AJI_CURRENT_VERSION`` in libaji's ``jtag_message.h``.
AJI_CURRENT_VERSION = 13

#: Greeting "JTAG Server\r\n" — exact bytes a libaji client expects.
AJI_SIGNATURE = "JTAG Server\r\n"

#: Default TCP port for the AJI service.
JTAG_PORT = 1309

#: Maximum payload of a single mux packet (libaji asserts this).
MUX_MAX_PAYLOAD = 4096

#: Mux channel for command/response traffic.
MUX_COMMAND = 0

#: Mux channel range (FIFO_MIN..FIFO_MIN+11) used for FIFOs in libaji.
MUX_FIFO_MIN = 4
MUX_FIFO_MAX = 15


# --- Command codes (verbatim from libaji jtag_message.h) -------------------

class Command(enum.IntEnum):
    """AJI message opcodes. Values are authoritative from libaji."""

    # Hardware
    GET_HARDWARE             = 0x80   # 0+
    ADD_HARDWARE             = 0x81   # 0+
    GET_VERSION_INFO         = 0x83   # 2+
    GET_DEFINED_DEVICES      = 0x84   # 2+
    GET_QUARTUS_DEVICES      = 0x89   # 9+
    PROGRESS                 = 0x9E   # 0+
    OUTPUT                   = 0x9F   # 0+

    # Chain management
    REMOVE_HARDWARE          = 0xA1   # 0+
    LOCK_CHAIN               = 0xA2   # 0+
    UNLOCK_CHAIN             = 0xA3   # 0+
    SCAN_CHAIN               = 0xA4   # 0+
    READ_CHAIN               = 0xA5   # 0+
    DEFINE_DEVICE            = 0xA7   # 0+
    OPEN_DEVICE              = 0xA8   # 0+
    OPEN_ENTIRE_CHAIN        = 0xA9   # 1+
    SET_PARAMETER            = 0xAA   # 2+
    GET_PARAMETER            = 0xAB   # 2+
    GET_PARAMETER_BLOCK      = 0xAD   # 2+
    WATCH_DATA               = 0xBE   # 0+

    # Per-open-device operations
    CLOSE_DEVICE             = 0xC0   # 0+
    LOCK_DEVICE              = 0xC1   # 0+
    UNLOCK_DEVICE            = 0xC2   # 0+
    ACCESS_IR                = 0xC5   # 0+
    ACCESS_DR                = 0xC7   # 0+
    ANY_SEQUENCE             = 0xC9   # [1-2] 3+
    RUN_TEST_IDLE            = 0xCA   # 0+
    TEST_LOGIC_RESET         = 0xCB   # 0+
    DELAY_MICROSECONDS       = 0xCE   # 0+
    ACCESS_IR_FIFO           = 0xCF   # 1+
    UNLOCK_LOCK_DEVICE       = 0xD3   # 2+

    # Authentication / housekeeping
    AUTHENTICATE_MD5         = 0xF0   # 0+
    CHECK_PASSWORD           = 0xF1   # 10+
    PING                     = 0xFD   # 12+ command, notify ignored by all
    USE_PROTOCOL_VERSION     = 0xFE   # 2+
    CONTINUE_COMMANDS        = 0xFF   # 0+


class HwType(enum.IntEnum):
    """Hardware-type tag used by ADD_HARDWARE."""

    OTHER         = 0
    BYTEBLASTER   = 1
    MASTERBLASTER = 2  # deprecated


class ServerFlags(enum.IntFlag):
    """Bits returned in the GET_VERSION_INFO response payload."""

    NONE         = 0
    ALLOW_REMOTE = 1


# --- Mux framing -----------------------------------------------------------

def encode_mux_header(mux: int, payload_len: int) -> bytes:
    """Encode the 2-byte mux header for ``mux`` and a payload of length
    ``payload_len``. Mirrors ``TCPLINK::add_packet`` in libaji.
    """
    if not (0 <= mux <= 15):
        raise ValueError(f"mux must be 0..15, got {mux}")
    if not (1 <= payload_len <= MUX_MAX_PAYLOAD):
        raise ValueError(
            f"payload_len must be 1..{MUX_MAX_PAYLOAD}, got {payload_len}")
    header = ((mux & 0xF) << 12) | ((payload_len - 1) & 0xFFF)
    return struct.pack(">H", header)


def decode_mux_header(header: bytes) -> tuple[int, int]:
    """Decode a 2-byte mux header.

    Returns ``(mux, payload_len)``. Inverse of :func:`encode_mux_header`.
    """
    if len(header) < 2:
        raise ValueError("mux header is 2 bytes")
    raw = struct.unpack(">H", header[:2])[0]
    mux = (raw >> 12) & 0xF
    payload_len = (raw & 0xFFF) + 1
    return mux, payload_len


def encode_mux_packet(mux: int, payload: bytes) -> bytes:
    return encode_mux_header(mux, len(payload)) + payload


# --- MESSAGE primitives ----------------------------------------------------

class MessageBuilder:
    """Builds a stream of length-prefixed AJI command blocks.

    Mirrors libaji's ``TXMESSAGE``. The block layout is:

        [cmd(1)][reserved=0(1)][total_len_be(2)][payload...]

    where ``total_len`` includes the 4-byte header.
    """

    def __init__(self) -> None:
        self.__buf = bytearray()
        # offset of the current open block's length field, or None if none
        self.__block_len_at: int | None = None

    def __close_block(self) -> None:
        if self.__block_len_at is None:
            return
        block_start = self.__block_len_at - 2
        block_end = len(self.__buf)
        block_len = block_end - block_start
        if block_len > 0xFFFF:
            raise ValueError(f"block too large: {block_len}")
        struct.pack_into(">H", self.__buf, self.__block_len_at, block_len)
        self.__block_len_at = None

    def add_command(self, cmd: Command) -> Self:
        self.__close_block()
        self.__buf.append(int(cmd))
        self.__buf.append(0)              # reserved
        self.__block_len_at = len(self.__buf)
        self.__buf.extend(b"\x00\x00")    # total_len placeholder
        return self

    def start_response(self, status: int) -> Self:
        """Begin a response block. ``status`` is an AJI error code."""
        self.__close_block()
        self.__buf.append(int(status))
        self.__buf.append(0)
        self.__block_len_at = len(self.__buf)
        self.__buf.extend(b"\x00\x00")
        return self

    def add_int(self, value: int) -> Self:
        self.__buf.extend(struct.pack(">I", value & 0xFFFFFFFF))
        return self

    def add_long(self, value: int) -> Self:
        self.__buf.extend(struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF))
        return self

    def add_string(self, s: str) -> Self:
        data = s.encode("latin-1")
        if len(data) > 0xFF:
            raise ValueError(f"string too long: {len(data)} bytes")
        self.__buf.append(len(data))
        self.__buf.extend(data)
        return self

    def add_raw(self, data: bytes) -> Self:
        self.__buf.extend(data)
        return self

    def build(self) -> bytes:
        self.__close_block()
        return bytes(self.__buf)


class MessageReader:
    """Stateful reader over concatenated AJI blocks.

    Mirrors libaji's ``RXMESSAGE``. Each block starts with the 4-byte
    header described above. ``next_block`` advances to the next one
    and returns the block's command/status; subsequent ``read_*`` calls
    consume bytes from that block's payload.
    """

    def __init__(self, data: bytes) -> None:
        self.__data = data
        self.__pos = 0
        self.__block_end = 0  # end of the current block (exclusive)

    @property
    def remaining(self) -> int:
        """Bytes remaining inside the current block."""
        return self.__block_end - self.__pos

    @property
    def at_end(self) -> bool:
        """True if there are no more blocks to read."""
        return self.__block_end >= len(self.__data)

    def next_block(self) -> int:
        """Skip any unconsumed payload of the current block, then read
        the next block's 4-byte header. Returns the cmd/status byte.
        """
        # Skip past any unconsumed bytes of the current block.
        self.__pos = self.__block_end
        if self.__pos + 4 > len(self.__data):
            raise EOFError("truncated block header")
        cmd = self.__data[self.__pos]
        # byte+1 is reserved; bytes+2..+4 are the BE total_len.
        total_len = struct.unpack_from(">H", self.__data, self.__pos + 2)[0]
        if total_len < 4:
            raise ValueError(f"invalid block total_len {total_len}")
        if self.__pos + total_len > len(self.__data):
            raise ValueError(
                f"block extends past buffer ({total_len} bytes asked, "
                f"{len(self.__data) - self.__pos} available)")
        self.__block_end = self.__pos + total_len
        self.__pos += 4
        return cmd

    def read_int(self) -> int:
        if self.remaining < 4:
            raise EOFError("not enough data for int")
        v = struct.unpack_from(">I", self.__data, self.__pos)[0]
        self.__pos += 4
        return v

    def read_long(self) -> int:
        if self.remaining < 8:
            raise EOFError("not enough data for long")
        v = struct.unpack_from(">Q", self.__data, self.__pos)[0]
        self.__pos += 8
        return v

    def read_string(self) -> str:
        if self.remaining < 1:
            raise EOFError("not enough data for string length")
        n = self.__data[self.__pos]
        if self.remaining < 1 + n:
            raise EOFError("string runs past block end")
        s = self.__data[self.__pos + 1:self.__pos + 1 + n].decode(
            "latin-1", errors="replace")
        self.__pos += 1 + n
        return s

    def read_raw(self, n: int) -> bytes:
        if self.remaining < n:
            raise EOFError(f"not enough data for {n} raw bytes")
        out = bytes(self.__data[self.__pos:self.__pos + n])
        self.__pos += n
        return out


# --- Greeting --------------------------------------------------------------

class Greeting:
    """Server greeting parsed from the first block on mux 0."""

    __slots__ = ("server_version", "authtype", "challenge")

    def __init__(self, server_version: int, authtype: int,
                 challenge: bytes | None) -> None:
        self.server_version = server_version
        self.authtype = authtype
        self.challenge = challenge

    def __repr__(self) -> str:
        return (f"Greeting(version={self.server_version}, "
                f"authtype={self.authtype}, "
                f"challenge={self.challenge!r})")


def parse_greeting(payload: bytes) -> Greeting:
    """Decode a greeting block payload (the bytes after the 4-byte
    block header in the first packet from the server).

    Mirrors the parsing in ``AJI_CLIENT::initial_negotiation``.
    """
    # The greeting is laid out as a single MESSAGE block. Wrap the
    # payload in a fake block so MessageReader can chew through it.
    fake = bytearray()
    fake.append(0)            # cmd byte (unused for greeting)
    fake.append(0)            # reserved
    fake.extend(struct.pack(">H", 4 + len(payload)))
    fake.extend(payload)
    rdr = MessageReader(bytes(fake))
    rdr.next_block()
    signature = rdr.read_string()
    if signature != AJI_SIGNATURE:
        raise ValueError(f"unexpected greeting signature {signature!r}")
    server_version = rdr.read_int()
    if server_version > 0xFFFF:
        raise ValueError(f"server version {server_version} out of range")
    authtype = rdr.read_int()
    challenge = None
    if authtype == int(Command.AUTHENTICATE_MD5):
        challenge = rdr.read_raw(16)
    return Greeting(server_version, authtype, challenge)


def build_greeting(server_version: int = AJI_CURRENT_VERSION,
                   authtype: int = 0,
                   challenge: bytes | None = None) -> bytes:
    """Build the greeting payload (everything *after* the 4-byte block
    header). Use this when implementing a server.
    """
    body = bytearray()
    body.append(len(AJI_SIGNATURE))
    body.extend(AJI_SIGNATURE.encode("latin-1"))
    body.extend(struct.pack(">I", server_version))
    body.extend(struct.pack(">I", authtype))
    if authtype == int(Command.AUTHENTICATE_MD5):
        if challenge is None or len(challenge) != 16:
            raise ValueError("AUTHENTICATE_MD5 needs a 16-byte challenge")
        body.extend(challenge)
    return bytes(body)
