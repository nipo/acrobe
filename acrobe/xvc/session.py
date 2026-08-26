"""Per-connection XVC session driver.

A :class:`XvcSession` owns one TCP socket and bridges its three
commands to a :class:`JtagInterface`:

* ``getinfo:`` — replies with the server-version banner.
* ``settck:`` — adjusts the JTAG clock cap under a per-session key.
* ``shift:`` — feeds (TMS, TDI) bursts into a :class:`JtagTmsWalker`
  and returns the captured TDO bits.
"""

import asyncio
import logging

from ..bitstring import BitString
from ..protocol import jtag
from ..protocol.jtag_walker import JtagTmsWalker

from . import wire


_logger = logging.getLogger("xvc.session")

# Frequency-cap key tagging XVC client requests in the FreqCapper
# constraints map. Removed when the session ends.
FREQ_CAP_KEY = "xvc"


class XvcSession:
    """One connected XVC client. Run by :class:`XvcListener`."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter,
                 interface: jtag.JtagInterface,
                 logger: logging.Logger | None = None) -> None:
        self._reader = reader
        self._writer = writer
        self._interface = interface
        self._walker = JtagTmsWalker(interface)
        self.logger = logger or _logger

    async def serve(self) -> None:
        """Process commands until the peer disconnects."""
        try:
            while True:
                prefix = await self._read_prefix()
                if prefix is None:
                    return
                if prefix == wire.CMD_GETINFO:
                    await self._handle_getinfo()
                elif prefix == wire.CMD_SETTCK:
                    await self._handle_settck()
                elif prefix == wire.CMD_SHIFT:
                    await self._handle_shift()
                else:
                    # Unknown prefix — drop the client. There is no
                    # error frame in XVC; closing is the only signal.
                    self.logger.warning(
                        "Unknown XVC command prefix %r — closing session",
                        prefix)
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            self._interface.freq_cap(FREQ_CAP_KEY)

    async def _read_prefix(self) -> bytes | None:
        """Read up to MAX_PREFIX_LEN bytes and match a command prefix.

        Returns the matched prefix (already consumed from the stream)
        or None on clean EOF before any byte arrived.
        """
        # Peek-by-read: every command prefix is unique within the set
        # and ends with ':'. We accumulate bytes and match greedily.
        buf = bytearray()
        while len(buf) < wire.MAX_PREFIX_LEN:
            chunk = await self._reader.read(1)
            if not chunk:
                if not buf:
                    return None
                raise asyncio.IncompleteReadError(bytes(buf), None)
            buf += chunk
            for prefix in wire.COMMAND_PREFIXES:
                if bytes(buf) == prefix:
                    return prefix
        # Read MAX_PREFIX_LEN bytes without matching anything.
        self.logger.warning("XVC command does not start with a known "
                            "prefix (got %r)", bytes(buf))
        return b""

    async def _handle_getinfo(self) -> None:
        self.logger.debug("getinfo: -> %r", wire.GETINFO_RESPONSE)
        self._writer.write(wire.GETINFO_RESPONSE)
        await self._writer.drain()

    async def _handle_settck(self) -> None:
        payload = await self._reader.readexactly(4)
        period_ns = wire.decode_settck_request(payload)
        if period_ns == 0:
            # 0 ns is meaningless — the client typically sends it to
            # query "what's your fastest". Treat as "uncap" and report
            # back whatever the interface settles on.
            requested = None
        else:
            requested = 1.0 / (period_ns * 1e-9)
        achieved = self._interface.freq_cap(FREQ_CAP_KEY, requested)
        # Report back the period the interface actually settled on.
        if achieved is None or achieved <= 0:
            response_ns = period_ns  # echo whatever was asked
        else:
            response_ns = max(1, int(round(1e9 / achieved)))
        self.logger.debug("settck: requested=%s ns, replying %d ns",
                          period_ns, response_ns)
        self._writer.write(wire.encode_settck_response(response_ns))
        await self._writer.drain()

    async def _handle_shift(self) -> None:
        header = await self._reader.readexactly(4)
        nbits = wire.decode_shift_header(header)
        nbytes = (nbits + 7) // 8
        if nbytes > wire.MAX_SHIFT_BYTES:
            self.logger.error(
                "shift: request of %d bits (%d bytes) exceeds advertised "
                "max %d bytes — dropping client",
                nbits, nbytes, wire.MAX_SHIFT_BYTES)
            raise ConnectionResetError("oversized shift")
        tms_bytes = await self._reader.readexactly(nbytes)
        tdi_bytes = await self._reader.readexactly(nbytes)
        tms = BitString(tms_bytes, nbits)
        tdi = BitString(tdi_bytes, nbits)
        self.logger.debug("shift: %d bits", nbits)
        tdo = await self._walker.process(tms, tdi)
        if len(tdo) != nbits:
            raise RuntimeError(
                f"walker returned {len(tdo)} TDO bits, expected {nbits}")
        # The wire format is byte-padded; pad TDO with zeros up to the
        # next byte boundary if needed.
        out = tdo.data
        if len(out) < nbytes:
            out = out + b"\0" * (nbytes - len(out))
        self._writer.write(out)
        await self._writer.drain()
