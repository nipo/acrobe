"""NSL bnoc committed channel.

Matches RTL ``nsl_bnoc.committed``: every transmitted frame is
appended a one-byte commit/cancel trailer (``0x01`` for commit,
``0x00`` for cancel). The wrapper is itself a `Datagram` — callers
just see a plain framed channel and never have to think about the
trailer byte.

On receive, cancelled frames (trailer ``0x00``) are logged at WARNING
and silently dropped; the next valid frame from the backend is used
to satisfy the pending Recv.
"""

import asyncio

from ....protocol.datagram import Datagram, Send, Recv


class Committed(Datagram):
    """Atomic frame delivery on top of another `Datagram` channel."""

    COMMIT = 0x01
    CANCEL = 0x00

    def __init__(self, channel: Datagram, name: str = "committed"):
        super().__init__(name)
        self.__channel = channel

    async def flush_ops(self, batch):
        lower: list = []
        for op, future in batch:
            if isinstance(op, Send):
                lf = self.__channel.post(
                    Send(op.data + bytes([self.COMMIT]), op.context))
                lower.append((lf, future, "send"))
            elif isinstance(op, Recv):
                lf = self.__channel.post(Recv(op.context))
                lower.append((lf, future, "recv", op.context))

        # Await sends first.
        await asyncio.gather(*[entry[0] for entry in lower])

        for entry in lower:
            if entry[2] == "send":
                lf, mf, _ = entry
                if mf is not None and not mf.done():
                    mf.set_result(None)
                continue

            lf, mf, _, ctx = entry
            data, recv_ctx = lf.result()
            while data and data[-1] == self.CANCEL:
                self.logger.warning(
                    "Cancelled frame dropped (trailer 0x00, payload %d B)",
                    len(data) - 1)
                # Pull another frame synchronously from the backend.
                rlf = self.__channel.post(Recv(ctx))
                await rlf
                data, recv_ctx = rlf.result()
            if mf is None or mf.done():
                continue
            if not data or data[-1] != self.COMMIT:
                mf.set_exception(RuntimeError(
                    f"Committed: invalid trailer in {len(data)}-byte frame"))
                continue
            mf.set_result((data[:-1], recv_ctx))
