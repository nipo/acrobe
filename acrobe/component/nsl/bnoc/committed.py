"""NSL bnoc committed framed channel.

Matches RTL ``nsl_bnoc.committed``: every transmitted frame is
appended a one-byte commit/cancel trailer (``0x01`` for commit,
``0x00`` for cancel). The wrapper is a `Framed` itself — the caller
just sees a framed channel.
"""

import asyncio

from ....protocol.datagram import Send, Recv
from .framed import Framed


class Committed(Framed):
    """Atomic frame delivery on top of another `Framed` channel."""

    COMMIT = 0x01
    CANCEL = 0x00

    def __init__(self, channel: Framed, name: str = "committed"):
        super().__init__(name)
        self._channel = channel

    async def flush_ops(self, batch):
        lower_futures = []
        for op, future in batch:
            if isinstance(op, Send):
                lf = self._channel.send(op.data + bytes([self.COMMIT]))
                lower_futures.append((lf, future, 'send'))
            elif isinstance(op, Recv):
                lf = self._channel.recv()
                lower_futures.append((lf, future, 'recv'))

        await asyncio.gather(*[f for f, _, _ in lower_futures])

        for lf, mf, kind in lower_futures:
            if kind == 'send':
                if not mf.done():
                    mf.set_result(None)
            else:
                data, ctx = lf.result()
                if not mf.done():
                    mf.set_result((data, ctx))
