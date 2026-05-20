"""FT245 synchronous FIFO Pipe.

Wraps an :class:`~acrobe.adapter.ftdi.transport.FtdiTransport` already
opened in FT245 SYNCFF bitmode and exposes it as a
:class:`acrobe.protocol.pipe.Pipe`. The FTDI chip relays a transparent
byte stream between the USB bulk endpoints and the FPGA's FIFO
controller — there is no command framing on the wire, so callers above
the Pipe are responsible for any structure they need (typically the
NSL bnoc Sized layer).
"""

from __future__ import annotations

import asyncio

from ...protocol.pipe import Pipe, Read, Write


class Ft245SyncPipe(Pipe):
    """Pipe over an FTDI channel in FT245 Synchronous FIFO bitmode."""

    def __init__(self, transport, name: str = "fifo"):
        super().__init__(name)
        self.__transport = transport

    @property
    def transport(self):
        return self.__transport

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Write):
                try:
                    await self.__transport.write(op.data)
                except BaseException as exc:
                    if future is not None and not future.done():
                        future.set_exception(exc)
                    raise
                if future is not None and not future.done():
                    future.set_result(None)
                continue

            if isinstance(op, Read):
                if op.size is None:
                    raise NotImplementedError(
                        "Ft245SyncPipe does not support Pipe.read(size=None); "
                        "the FTDI transport has an exact-size contract")
                try:
                    data = await self.__transport.read(op.size)
                except BaseException as exc:
                    if future is not None and not future.done():
                        future.set_exception(exc)
                    raise
                if future is not None and not future.done():
                    future.set_result(bytes(data))
                continue

            raise TypeError(f"Ft245SyncPipe cannot handle {type(op).__name__}")
