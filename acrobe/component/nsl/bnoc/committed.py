import asyncio

from .framed import Framed, FrameSend, FrameRecv


class Committed(Framed):
    """Atomic frame delivery. Matches RTL nsl_bnoc_committed.

    Wraps another Framed channel, appending commit byte on send.
    """

    COMMIT = 0x01
    CANCEL = 0x00

    def __init__(self, channel: Framed, name: str = "committed"):
        super().__init__(name)
        self._channel = channel

    async def flush_ops(self, batch):
        lower_futures = []
        for op, future in batch:
            if isinstance(op, FrameSend):
                lf = self._channel.send(op.data + bytes([self.COMMIT]))
                lower_futures.append((lf, future, 'send'))
            elif isinstance(op, FrameRecv):
                lf = self._channel.recv()
                lower_futures.append((lf, future, 'recv'))

        await asyncio.gather(*[f for f, _, _ in lower_futures])

        for lf, mf, kind in lower_futures:
            r = lf.result()
            if kind == 'send':
                mf.set_result(FrameSend(r.data))
            else:
                recv = FrameRecv()
                recv.data = r.data
                mf.set_result(recv)
