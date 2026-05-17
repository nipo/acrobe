"""XvcClient — JtagInterface backed by a remote XVC server.

One instance owns one TCP connection. flush_ops translates each
batch through :class:`JtagOpEncoder`, sends one or more ``shift:``
commands (chunked to fit the server's advertised maximum), and
slices the returned TDO back into the originating Shift ops.

Path: ``xvc/<host>[:<port>]/<chain>/...``. The user must layer a
:class:`Chain` on top of the :class:`XvcClient`; the client itself
is a bit-level interface and doesn't enumerate hardware.
"""

import asyncio
import logging
import re
import struct

from ...bitstring import BitString
from ...protocol import jtag
from ...protocol.jtag_encoder import JtagOpEncoder
from ...xvc import wire


_logger = logging.getLogger("xvc.client")


DEFAULT_PORT = 2542


class XvcProtocolError(Exception):
    """Raised when the server returns something we can't parse."""


class XvcClient(jtag.JtagInterface):
    """Remote XVC server appearing as a local :class:`JtagInterface`."""

    def __init__(self, name: str, *, host: str, port: int = DEFAULT_PORT
                 ) -> None:
        super().__init__(name=name)
        self.__host = host
        self.__port = port
        self.__reader: asyncio.StreamReader | None = None
        self.__writer: asyncio.StreamWriter | None = None
        self.__encoder = JtagOpEncoder()
        self.__server_version: str = ""
        # Server-advertised max payload bytes per shift: command. Capped
        # by the client to a sane lower bound if the server advertises
        # something unrealistic.
        self.__max_shift_bytes: int = 1024
        # Serialise concurrent flushes — the wire is a single TCP
        # stream, request/response is interleaved, can't be reordered.
        self.__wire_lock = asyncio.Lock()

    # --- Lifecycle ---

    async def start(self) -> None:
        self.__reader, self.__writer = await asyncio.open_connection(
            self.__host, self.__port)
        _logger.info("connected to xvc://%s:%d", self.__host, self.__port)
        await self.__handshake()
        # If a freq cap was registered before we were connected (children
        # added to the tree before start_tree() reached us), push it now.
        if self.freq is not None:
            await self.__do_settck(max(1, int(round(1e9 / self.freq))))
        from ...lifecycle import on_shutdown
        on_shutdown(self.stop)

    async def stop(self) -> None:
        from ...lifecycle import cancel_shutdown
        cancel_shutdown(self.stop)
        writer = self.__writer
        self.__writer = None
        self.__reader = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def __handshake(self) -> None:
        """Send getinfo: and parse the server's version banner.

        The reply is ``xvcServer_v<MAJOR>.<MINOR>:<MAX_SHIFT_BYTES>\\n``.
        Anything else is fatal — the server is unusable.
        """
        assert self.__writer is not None and self.__reader is not None
        self.__writer.write(wire.CMD_GETINFO)
        await self.__writer.drain()
        # The banner is short and \n-terminated — readline is enough.
        line = await self.__reader.readline()
        if not line:
            raise XvcProtocolError("server closed before getinfo: reply")
        text = line.rstrip(b"\r\n").decode("ascii", errors="replace")
        m = re.match(r"^(xvcServer_v[0-9.]+):(\d+)$", text)
        if not m:
            raise XvcProtocolError(
                f"unexpected getinfo: reply {text!r}")
        self.__server_version = m.group(1)
        advertised = int(m.group(2))
        if advertised < 16:
            # Clearly bogus — keep the conservative default. Spec
            # doesn't mandate a minimum, but a single shift smaller
            # than a few bytes makes the protocol useless.
            _logger.warning("server advertises max_shift_bytes=%d (too "
                            "small); keeping client default of %d",
                            advertised, self.__max_shift_bytes)
        else:
            self.__max_shift_bytes = advertised
        _logger.info("xvc server %s, max_shift_bytes=%d",
                     self.__server_version, self.__max_shift_bytes)

    # --- Frequency control ---

    def freq_update(self, freq):
        """Negotiate TCK rate with the server via settck:.

        Synchronous from the FreqCapper's perspective, but the actual
        wire round-trip is async — we schedule a task and return the
        requested frequency optimistically. The server's reply
        adjusts our internal rate when it lands; subsequent flushes
        run at whatever the server settled on.
        """
        if freq is None or freq <= 0:
            return None
        if self.__writer is None:
            # Not connected yet; remember the request and apply on start.
            return freq
        # We have to push the new TCK before any subsequent shift:,
        # so do it synchronously by spawning a serialised task.
        period_ns = max(1, int(round(1e9 / freq)))
        task = asyncio.create_task(self.__do_settck(period_ns))
        # We don't await here — the caller is FreqCapper.__recalculate,
        # which is sync. The settck round-trip serialises behind the
        # wire_lock with any pending shift:.
        # Return the requested freq; the server's reply will refine it
        # on the next FreqCapper update if it ever differs significantly.
        # (Most XVC servers honour exactly what's asked.)
        task.add_done_callback(_log_settck_result)
        return freq

    async def __do_settck(self, period_ns: int) -> int:
        """Send settck: and return the achieved period (ns)."""
        async with self.__wire_lock:
            assert self.__writer is not None and self.__reader is not None
            self.__writer.write(wire.CMD_SETTCK
                                + struct.pack("<L", period_ns))
            await self.__writer.drain()
            reply = await self.__reader.readexactly(4)
            achieved_ns, = struct.unpack("<L", reply)
            _logger.debug("settck: requested %d ns, server replied %d ns",
                          period_ns, achieved_ns)
            return achieved_ns

    # --- Op flushing ---

    async def flush_ops(self, batch: list) -> None:
        """Lower a batch of ops into TMS/TDI, run them through the
        server, distribute TDO back into Shift ops."""
        if self.__writer is None:
            raise RuntimeError(
                f"XvcClient {self.name!r} not connected")

        ops = [op for op, _f in batch]
        try:
            tms, tdi, slots = self.__encoder.encode(ops)
        except Exception as exc:
            for _, future in batch:
                if not future.done():
                    future.set_exception(exc)
            return

        try:
            tdo = await self.__wire_shift(tms, tdi)
        except Exception as exc:
            for _, future in batch:
                if not future.done():
                    future.set_exception(exc)
            return

        # Slot TDO bits back, keyed by op identity. Ops that don't
        # read get None.
        captured: dict[int, BitString] = {}
        for shift_op, offset in slots:
            length = len(shift_op.tdi)
            captured[id(shift_op)] = tdo[offset:offset + length]

        for op, future in batch:
            if future.done():
                continue
            future.set_result(captured.get(id(op)))

    async def __wire_shift(self, tms: BitString, tdi: BitString
                           ) -> BitString:
        """Send one or more ``shift:`` commands covering the whole
        (tms, tdi) and concatenate TDO replies."""
        if len(tms) != len(tdi):
            raise ValueError(
                f"tms/tdi length mismatch: {len(tms)} vs {len(tdi)}")
        total_bits = len(tms)
        if total_bits == 0:
            return BitString()

        bits_per_chunk = self.__max_shift_bytes * 8
        result = BitString()
        async with self.__wire_lock:
            assert self.__writer is not None and self.__reader is not None
            for start in range(0, total_bits, bits_per_chunk):
                end = min(start + bits_per_chunk, total_bits)
                chunk_bits = end - start
                chunk_bytes = (chunk_bits + 7) // 8
                tms_slice = tms[start:end]
                tdi_slice = tdi[start:end]
                # BitString slices may produce a shorter .data when the
                # last byte is partial; pad to the byte count the
                # protocol expects.
                tms_data = tms_slice.data.ljust(chunk_bytes, b"\0")
                tdi_data = tdi_slice.data.ljust(chunk_bytes, b"\0")
                self.__writer.write(wire.CMD_SHIFT
                                    + struct.pack("<L", chunk_bits)
                                    + tms_data + tdi_data)
                await self.__writer.drain()
                tdo_bytes = await self.__reader.readexactly(chunk_bytes)
                result.append(tdo_bytes, chunk_bits)
        return result

    def __repr__(self) -> str:
        return (f"<XvcClient {self._name} {self.__host}:{self.__port} "
                f"server={self.__server_version!r}>")


def _log_settck_result(task: asyncio.Task) -> None:
    try:
        period_ns = task.result()
    except Exception as e:
        _logger.warning("settck: failed: %s", e)
        return
    _logger.debug("settck: server settled on %d ns", period_ns)
