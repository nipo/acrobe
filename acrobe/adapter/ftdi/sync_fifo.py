"""FT245 synchronous FIFO Pipe.

Wraps an :class:`~acrobe.adapter.ftdi.transport.FtdiTransport` already
opened in FT245 SYNCFF bitmode and exposes it as a
:class:`acrobe.protocol.pipe.Pipe`. The FTDI chip relays a transparent
byte stream between the USB bulk endpoints and the FPGA's FIFO
controller — there is no command framing on the wire, so callers above
the Pipe are responsible for any structure they need (typically the
NSL bnoc Sized or Hdlc layer).

Sized reads (``size=int``) route to the transport's exact-size
:meth:`~acrobe.adapter.ftdi.transport.FtdiTransport.read`; unsized
reads (``size=None``) route to
:meth:`~acrobe.adapter.ftdi.transport.FtdiTransport.read_some`,
which returns as soon as any bytes are available. Bytes pulled past
the requested boundary stay in the transport's internal residual
and surface on the next read of either kind.
"""

from __future__ import annotations

import asyncio

from ...protocol.pipe import Pipe, Read, Write


class Ft245SyncPipe(Pipe):
    """Pipe over an FTDI channel in FT245 Synchronous FIFO bitmode."""

    # Cap on a single read_some() call. Sized to comfortably exceed
    # the FT2232H bulk MPS (512 B) so any USB packet currently in
    # flight lands in one transport read.
    UNSIZED_READ_MAX = 4096

    def __init__(self, transport, name: str = "fifo"):
        super().__init__(name)
        self.__transport = transport

    @property
    def transport(self):
        return self.__transport

    async def flush_ops(self, batch):
        # Queue every op on the transport and return without awaiting.
        # The transport's writer/reader workers run independently, so a
        # large write and the reads draining its response overlap on the
        # wire. Awaiting here would hold the Batcher's flush lock for the
        # whole write, blocking the very read-flushes that must drain the
        # device's return path — a deadlock for a full-duplex FIFO
        # (loopback, request/response transactors) once a frame exceeds
        # the device's buffering. Each op's pipe-future is resolved from
        # its transport-future via a callback instead.
        for op, future in batch:
            if isinstance(op, Write):
                tf = asyncio.ensure_future(self.__transport.write(op.data))
                self.__wire(tf, future, is_read=False)
            elif isinstance(op, Read):
                if op.size is None:
                    tf = asyncio.ensure_future(
                        self.__transport.read_some(self.UNSIZED_READ_MAX))
                else:
                    tf = asyncio.ensure_future(
                        self.__transport.read(op.size))
                self.__wire(tf, future, is_read=True)
            else:
                raise TypeError(
                    f"Ft245SyncPipe cannot handle {type(op).__name__}")

    @staticmethod
    def __wire(transport_future, pipe_future, *, is_read):
        if pipe_future is None:
            return

        def done(tf):
            if pipe_future.done():
                return
            exc = tf.exception()
            if exc is not None:
                pipe_future.set_exception(exc)
            elif is_read:
                pipe_future.set_result(bytes(tf.result()))
            else:
                pipe_future.set_result(None)

        transport_future.add_done_callback(done)
