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

from ....engine import chain_future
from ....protocol.datagram import Datagram, Send, Recv

@Datagram.db.register("committed")
class Committed(Datagram):
    """Atomic frame delivery on top of another `Datagram` channel."""

    COMMIT = 0x01
    CANCEL = 0x00

    def __init__(self, channel: Datagram, name: str = "committed"):
        super().__init__(name)
        self.__channel = channel

    async def flush_ops(self, batch):
        # Non-blocking (see Batcher contract): lower every op, post it,
        # chain futures. Cancelled frames are re-fetched by re-posting a
        # Recv from within the callback — never by awaiting here.
        for op, future in batch:
            if isinstance(op, Send):
                lf = self.__channel.post(
                    Send(op.data + bytes([self.COMMIT]), op.context))
                chain_future(lf, future, lambda _r: None)
            elif isinstance(op, Recv):
                self.__recv(op.context, future)

    def __recv(self, context, future):
        lf = self.__channel.post(Recv(context))
        lf.add_done_callback(lambda f: self.__on_frame(context, future, f))

    def __on_frame(self, context, future, f):
        exc = f.exception()
        if exc is not None:
            if future is not None and not future.done():
                future.set_exception(exc)
            return
        data, recv_ctx = f.result()
        if data and data[-1] == self.CANCEL:
            self.logger.warning(
                "Cancelled frame dropped (trailer 0x00, payload %d B)",
                len(data) - 1)
            self.__recv(context, future)   # pull another frame
            return
        if future is None or future.done():
            return
        if not data or data[-1] != self.COMMIT:
            future.set_exception(RuntimeError(
                f"Committed: invalid trailer in {len(data)}-byte frame"))
            return
        future.set_result((data[:-1], recv_ctx))
