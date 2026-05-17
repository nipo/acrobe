"""asyncio transport for the libaji-faithful protocol.

Wraps an :class:`asyncio.StreamReader` / :class:`asyncio.StreamWriter`
pair with libaji's mux framing. Mirrors the parts of
``libaji_client/src/jtag/jtag_tcplink.cpp`` and
``jtag_client_link.cpp`` that are relevant to a client; we don't
need a full-blown ``TCPLINK`` because asyncio handles buffering and
backpressure for us.

Three layers are exposed:

* :class:`MuxStream` — read/write a single mux packet
  (``(channel, payload)``). Pure framing; no protocol semantics.
* :class:`AjiLink` — connect / greet / version-negotiate. After
  ``connect`` it's ready for command exchanges. This is the analogue
  of ``AJI_CLIENT::initial_negotiation``.
* :func:`AjiLink.send_receive` — write a batch of command blocks on
  channel 0 and read back the matching responses (the response
  packet is a single mux frame with one block per command).

Higher-level operations (``get_hardware``, ``scan_chain``,
``open_device``, ``access_ir/access_dr``, etc.) live in a separate
module and call :class:`AjiLink` underneath.
"""

import asyncio
import logging
from typing import Self

from .wire import (
    AJI_CURRENT_VERSION,
    AJI_SIGNATURE,
    Command,
    Greeting,
    JTAG_PORT,
    MUX_COMMAND,
    MUX_FIFO_MIN,
    MUX_FIFO_MAX,
    MUX_MAX_PAYLOAD,
    MessageBuilder,
    MessageReader,
    ServerFlags,
    decode_mux_header,
    encode_mux_header,
    parse_greeting,
)


_logger = logging.getLogger("aji.client")


# --- MuxStream -------------------------------------------------------------


class MuxStream:
    """asyncio framing for libaji's mux protocol.

    Constructed from an open ``(reader, writer)`` pair; offers
    ``read_packet()`` and ``write_packet(mux, payload)`` matching the
    16-bit BE header described in :mod:`.wire`.
    """

    def __init__(self,
                 reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self.__reader = reader
        self.__writer = writer

    @property
    def closed(self) -> bool:
        return self.__writer.is_closing()

    async def read_packet(self) -> tuple[int, bytes]:
        """Read a single mux packet. Returns ``(mux, payload)``."""
        header = await self.__reader.readexactly(2)
        mux, length = decode_mux_header(header)
        payload = await self.__reader.readexactly(length)
        return mux, payload

    async def write_packet(self, mux: int, payload: bytes) -> None:
        if not (1 <= len(payload) <= MUX_MAX_PAYLOAD):
            raise ValueError(
                f"payload length {len(payload)} out of 1..{MUX_MAX_PAYLOAD}")
        self.__writer.write(encode_mux_header(mux, len(payload)) + payload)
        await self.__writer.drain()

    async def write_chunked(self, mux: int, payload: bytes) -> None:
        """Write a payload of arbitrary size by splitting it into mux
        frames of up to ``MUX_MAX_PAYLOAD`` bytes each. Mirrors libaji's
        TCPLINK::send_fifo, which carves the FIFO data into chunks
        bounded by the mux header's 12-bit length field.
        """
        if not payload:
            return
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:offset + MUX_MAX_PAYLOAD]
            self.__writer.write(
                encode_mux_header(mux, len(chunk)) + chunk)
            offset += len(chunk)
        await self.__writer.drain()

    async def close(self) -> None:
        try:
            self.__writer.close()
            await self.__writer.wait_closed()
        except Exception:
            pass


# --- AjiLink ---------------------------------------------------------------


class AjiLink:
    """High-level AJI link with greeting + version negotiation.

    Use :meth:`connect` to set one up. Once connected, callers send
    batches of command blocks via :meth:`send_receive`; FIFO reads on
    other mux channels are also available with :meth:`read_fifo` for
    the GET_HARDWARE / READ_CHAIN flows that push data on a separate
    channel before answering on the command channel.

    This class does *not* multiplex: callers must serialize their own
    requests if they share a single link. This matches libaji's
    ``AJI_CLIENT`` which uses a mutex (``link_is_claimed``) for the
    same purpose.
    """

    def __init__(self, mux: MuxStream) -> None:
        self.__mux = mux
        self.__lock = asyncio.Lock()
        self.__server_version: int = 0
        self.__server_flags: int = 0
        self.__server_version_info: str = ""
        self.__server_path: str = ""
        self.__pgmparts_version: int = 0

    # --- Properties populated after connect() ---

    @property
    def server_version(self) -> int:
        return self.__server_version

    @property
    def server_flags(self) -> int:
        return self.__server_flags

    @property
    def server_version_info(self) -> str:
        return self.__server_version_info

    @property
    def server_path(self) -> str:
        return self.__server_path

    @property
    def pgmparts_version(self) -> int:
        return self.__pgmparts_version

    @property
    def closed(self) -> bool:
        return self.__mux.closed

    # --- Lifecycle ---

    @classmethod
    async def connect(
        cls,
        host: str = "localhost",
        port: int = JTAG_PORT,
        *,
        password: str | None = None,
        client_version: int = AJI_CURRENT_VERSION,
    ) -> Self:
        reader, writer = await asyncio.open_connection(host, port)
        mux = MuxStream(reader, writer)
        link = cls(mux)
        try:
            await link.__negotiate(password=password,
                                   client_version=client_version)
        except Exception:
            await mux.close()
            raise
        return link

    async def close(self) -> None:
        await self.__mux.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # --- send / receive on command channel ---

    async def send_receive(
        self,
        request_blocks: bytes,
    ) -> bytes:
        """Send a batch of one or more command blocks (already serialised
        with :class:`MessageBuilder`) on the command channel and read
        back the response packet (also a single mux frame possibly
        carrying multiple response blocks).

        FIFO frames that arrive before the response on channel 0 are
        buffered and returned via :meth:`drain_fifos`; this happens
        for GET_HARDWARE, READ_CHAIN and similar paginated commands.
        Returns just the bytes of the response packet on channel 0.
        """
        async with self.__lock:
            self.__fifo_buffer = []
            await self.__mux.write_packet(MUX_COMMAND, request_blocks)
            return await self.__read_until_command_channel()

    async def send_receive_with_fifo(
        self,
        request_blocks: bytes,
        *,
        fifo_after: bytes = b"",
        fifo_mux: int = MUX_FIFO_MIN,
    ) -> bytes:
        """Send command blocks then immediately follow up with FIFO
        bytes on ``fifo_mux`` (split into as many ≤4096-byte mux
        frames as needed), then await the response packet.

        ACCESS_DR's write data uses this — libaji emits the command
        first so the server learns the FIFO byte count, then the
        FIFO data follows.
        """
        async with self.__lock:
            self.__fifo_buffer = []
            await self.__mux.write_packet(MUX_COMMAND, request_blocks)
            if fifo_after:
                await self.__mux.write_chunked(fifo_mux, fifo_after)
            return await self.__read_until_command_channel()

    async def __read_until_command_channel(self) -> bytes:
        """Read packets, stashing FIFO data until a command-channel
        packet arrives. Returns its payload.
        """
        while True:
            mux, payload = await self.__mux.read_packet()
            if mux == MUX_COMMAND:
                return payload
            if MUX_FIFO_MIN <= mux <= MUX_FIFO_MAX:
                # FIFO frame; remember which channel it came on.
                self.__fifo_buffer.append((mux, payload))
                continue
            _logger.warning("ignoring unsolicited packet on mux=%d (%d bytes)",
                            mux, len(payload))

    def drain_fifos(self) -> list[tuple[int, bytes]]:
        """Return the FIFO frames accumulated during the last
        send_receive() call, then clear the buffer.
        """
        out, self.__fifo_buffer = list(self.__fifo_buffer), []
        return out

    # --- Greeting / negotiation ---

    async def __negotiate(self, *, password: str | None,
                          client_version: int) -> None:
        # 1) Receive the greeting on channel 0.
        mux, payload = await self.__mux.read_packet()
        if mux != MUX_COMMAND:
            raise ConnectionError(
                f"expected greeting on mux 0, got mux={mux}")
        greeting = parse_greeting(payload)
        _logger.info("greeting: server_version=%d authtype=%d",
                     greeting.server_version, greeting.authtype)

        # 2) Optional MD5 challenge/response.
        if greeting.authtype == int(Command.AUTHENTICATE_MD5):
            if password is None:
                raise PermissionError(
                    "server requires authentication but no password provided")
            assert greeting.challenge is not None
            await self.__md5_authenticate(greeting.challenge, password)

        # 3) Pick a protocol version both sides understand. libaji
        #    clamps the server-advertised version to its own current
        #    version (we do the same).
        version = min(greeting.server_version, client_version)
        if version < 2:
            # Legacy: no USE_PROTOCOL_VERSION exchange, just stop.
            self.__server_version = version
            return
        self.__server_version = version

        # 4) Version 2+: send USE_PROTOCOL_VERSION + GET_VERSION_INFO
        #    batched, expect two responses in one packet.
        request = (MessageBuilder()
                   .add_command(Command.USE_PROTOCOL_VERSION)
                   .add_int(version)
                   .add_command(Command.GET_VERSION_INFO)
                   .build())
        response = await self.send_receive(request)
        rdr = MessageReader(response)

        # First response: server flags (1 int).
        status1 = rdr.next_block()
        if status1 != 0:
            raise ConnectionError(
                f"USE_PROTOCOL_VERSION returned status {status1}")
        self.__server_flags = rdr.read_int()

        # Second response: version_info string + pgmparts_version + server_path.
        status2 = rdr.next_block()
        if status2 != 0:
            raise ConnectionError(
                f"GET_VERSION_INFO returned status {status2}")
        self.__server_version_info = rdr.read_string()
        # pgmparts_version is optional and ignored by libaji clients.
        if rdr.remaining >= 4:
            self.__pgmparts_version = rdr.read_int()
        if rdr.remaining >= 1:
            try:
                self.__server_path = rdr.read_string()
            except EOFError:
                pass

        _logger.info("negotiated v=%d, flags=0x%x, info=%r",
                     self.__server_version, self.__server_flags,
                     self.__server_version_info)

    async def __md5_authenticate(self, challenge: bytes, password: str) -> None:
        import hashlib
        digest = hashlib.md5(challenge + password.encode("latin-1")).digest()
        request = (MessageBuilder()
                   .add_command(Command.AUTHENTICATE_MD5)
                   .add_raw(digest)
                   .build())
        response = await self.send_receive(request)
        rdr = MessageReader(response)
        status = rdr.next_block()
        if status != 0:
            raise PermissionError(f"authentication failed: status {status}")
