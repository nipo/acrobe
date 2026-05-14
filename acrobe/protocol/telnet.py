"""Generic Telnet (RFC 854/855) layer wrapping a Pipe.

TelnetPipe accepts a lower Pipe as transport and itself presents a
Pipe interface to the application (data with IAC stripped/escaped).
Options are attached as TelnetOption subclass instances; their
method presence drives dispatch. Unattached options are auto-refused.
"""

import asyncio
from typing import Any

from ..node import Node
from .pipe import Pipe, Read, Write


# Telnet control bytes (RFC 854)
IAC  = 0xFF
DONT = 0xFE
DO   = 0xFD
WONT = 0xFC
WILL = 0xFB
SB   = 0xFA
SE   = 0xF0

# Negotiation verb names for readable logs / dispatch
_NAME = {DO: "DO", DONT: "DONT", WILL: "WILL", WONT: "WONT", SB: "SB"}


class TelnetOption:
    """Base class for Telnet option handlers.

    Subclasses set `code` (and optionally `name`) and implement any of:
      - async peer_do(self, telnet)   — peer says DO <this option>
      - async peer_dont(self, telnet)
      - async peer_will(self, telnet)
      - async peer_wont(self, telnet)
      - async peer_sb(self, telnet, payload: bytes)

    Methods that aren't implemented cause the TelnetPipe to refuse
    the negotiation (reply WONT to DO, DONT to WILL; SB for an
    option with no peer_sb is silently dropped).
    """

    code: int = -1
    name: str = ""


class TelnetPipe(Pipe):
    """Pipe wrapping a lower Pipe, transparently handling Telnet IAC.

    - write(): escapes 0xFF bytes in user data.
    - read(): returns user data only; IAC sequences are handled
      concurrently by a background reader task.
    - option_add(opt): register an option handler by its code.
    """

    def __init__(self, transport: Pipe, logger=None, name: str = "telnet"):
        # ``logger`` kept as a kwarg for callers that want a specific
        # logger object (e.g. the rfc2217 listener tags telnet logs
        # under its own connection logger). Default falls back to the
        # Node fqdn-derived logger.
        super().__init__(name)
        self._transport = transport
        if logger is not None:
            self.__logger_override = logger
        else:
            self.__logger_override = None
        self._options: dict[int, TelnetOption] = {}
        self._rx_buf = bytearray()       # pending user-data bytes
        self._rx_event = asyncio.Event()  # set when data or EOF available
        self._eof = False
        self._reader_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False

    @property
    def logger(self):
        if self.__logger_override is not None:
            return self.__logger_override
        return Node.logger.fget(self)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Node-lifecycle start. Spins up the background IAC reader."""
        self._ensure_reader()

    def _ensure_reader(self):
        """Idempotently launch the background reader task. Must be
        called from inside a running event loop. Safe to call from
        ``flush_ops`` and from explicit ``start()``."""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self):
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

    def option_add(self, option: TelnetOption):
        assert 0 <= option.code <= 255
        self._options[option.code] = option

    # ------------------------------------------------------------------
    # Pipe interface (Batcher flush)
    # ------------------------------------------------------------------

    async def flush_ops(self, batch):
        # Writes are bounded — they hand off to the lower transport
        # and resolve quickly. Reads can block indefinitely waiting
        # for incoming data, so they get spawned as background tasks
        # so concurrent writes (or other reads) aren't held up by the
        # Batcher's serialization lock.
        self._ensure_reader()
        for op, future in batch:
            if isinstance(op, Write):
                escaped = op.data.replace(bytes([IAC]), bytes([IAC, IAC]))
                try:
                    async with self._write_lock:
                        await self._transport.write(escaped)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                    continue
                if not future.done():
                    future.set_result(None)
            elif isinstance(op, Read):
                asyncio.create_task(self._read_task(op.size, future))
            else:
                if not future.done():
                    future.set_exception(TypeError(
                        f"TelnetPipe: unsupported op {type(op).__name__}"))

    async def _read_task(self, size, future):
        try:
            data = await self._read_exact(size)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            return
        if not future.done():
            future.set_result(data)

    async def _read_exact(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            if self._rx_buf:
                take = min(size - len(out), len(self._rx_buf))
                out += self._rx_buf[:take]
                del self._rx_buf[:take]
                continue
            if self._eof:
                raise EOFError("Telnet transport closed")
            self._rx_event.clear()
            await self._rx_event.wait()
        return bytes(out)

    # ------------------------------------------------------------------
    # Raw IAC transmission — used by TelnetOption implementations
    # ------------------------------------------------------------------

    async def send_iac(self, *parts: int) -> None:
        """Send a 2- or 3-byte IAC sequence (e.g. IAC, WILL, 44)."""
        buf = bytes([IAC, *parts])
        async with self._write_lock:
            await self._transport.write(buf)

    async def send_sb(self, option: int, payload: bytes) -> None:
        """Send IAC SB <option> <payload> IAC SE, escaping IAC in payload."""
        escaped = payload.replace(bytes([IAC]), bytes([IAC, IAC]))
        buf = bytes([IAC, SB, option]) + escaped + bytes([IAC, SE])
        async with self._write_lock:
            await self._transport.write(buf)

    # ------------------------------------------------------------------
    # Background reader — parses IAC out of the stream
    # ------------------------------------------------------------------

    async def _reader_loop(self):
        NORMAL, GOT_IAC, GOT_VERB, GOT_SB, IN_SB, SB_IAC = range(6)

        state = NORMAL
        verb = 0
        sb_opt = 0
        sb_payload = bytearray()

        try:
            while not self._closed:
                chunk = await self._read_some()
                if not chunk:
                    self._eof = True
                    self._rx_event.set()
                    break

                for byte in chunk:
                    if state == NORMAL:
                        if byte == IAC:
                            state = GOT_IAC
                        else:
                            self._rx_buf.append(byte)

                    elif state == GOT_IAC:
                        if byte == IAC:
                            # Escaped 0xFF in data stream
                            self._rx_buf.append(IAC)
                            state = NORMAL
                        elif byte in (DO, DONT, WILL, WONT):
                            verb = byte
                            state = GOT_VERB
                        elif byte == SB:
                            state = GOT_SB
                        else:
                            # Unknown or 2-byte command (NOP, etc.) — ignore
                            self.logger.debug("Telnet: ignoring IAC %d", byte)
                            state = NORMAL

                    elif state == GOT_VERB:
                        await self._handle_verb(verb, byte)
                        state = NORMAL

                    elif state == GOT_SB:
                        sb_opt = byte
                        sb_payload.clear()
                        state = IN_SB

                    elif state == IN_SB:
                        if byte == IAC:
                            state = SB_IAC
                        else:
                            sb_payload.append(byte)

                    elif state == SB_IAC:
                        if byte == IAC:
                            sb_payload.append(IAC)
                            state = IN_SB
                        elif byte == SE:
                            await self._handle_sb(sb_opt, bytes(sb_payload))
                            state = NORMAL
                        else:
                            # Out-of-spec but tolerate: treat as end of SB
                            self.logger.debug(
                                "Telnet: unexpected IAC %d mid-SB", byte)
                            await self._handle_sb(sb_opt, bytes(sb_payload))
                            state = NORMAL

                self._rx_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.debug("Telnet reader exited: %r", e)
            self._eof = True
            self._rx_event.set()

    async def _read_some(self) -> bytes:
        # Pipe.read is "exactly size" — we want "some bytes". Read in
        # chunks of 1 and accumulate nothing (1-byte reads are fine
        # for a framing layer that uses byte-accurate lookahead).
        try:
            return await self._transport.read(1)
        except (EOFError, ConnectionError):
            return b""

    async def _handle_verb(self, verb: int, opt_code: int):
        self.logger.debug("Telnet << IAC %s %d", _NAME.get(verb, verb), opt_code)
        opt = self._options.get(opt_code)
        if opt is None:
            # No handler: refuse politely
            if verb == DO:
                await self.send_iac(WONT, opt_code)
            elif verb == WILL:
                await self.send_iac(DONT, opt_code)
            # DONT / WONT: nothing to answer
            return
        method_name = {
            DO: "peer_do", DONT: "peer_dont",
            WILL: "peer_will", WONT: "peer_wont",
        }[verb]
        method = getattr(opt, method_name, None)
        if method is None:
            # Handler registered but this verb not implemented → refuse
            if verb == DO:
                await self.send_iac(WONT, opt_code)
            elif verb == WILL:
                await self.send_iac(DONT, opt_code)
            return
        await method(self)

    async def _handle_sb(self, opt_code: int, payload: bytes):
        self.logger.debug("Telnet << IAC SB %d [%d bytes]", opt_code, len(payload))
        opt = self._options.get(opt_code)
        if opt is None:
            return
        method = getattr(opt, "peer_sb", None)
        if method is None:
            return
        await method(self, payload)


