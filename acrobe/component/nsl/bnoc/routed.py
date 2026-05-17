"""NSL bnoc routed channel.

Matches RTL ``nsl_bnoc.routed``: a single framed channel multiplexes
several point-to-point endpoints. Each frame on the wire carries a
one-byte routing header ``[dst(3:0) | src(7:4)]`` followed by the
payload.

`Router` is the multiplexer (a `Datagram` with routing context).
`Route` is a sugar wrapper that pins one (local, remote) endpoint
pair on a `Router` and exposes itself as a `Framed` channel (no
routing visible to its users).
"""

from collections import defaultdict, deque
from dataclasses import dataclass

import asyncio

from ....protocol.datagram import Datagram, Send, Recv
from .framed import Framed


@dataclass(frozen=True, slots=True)
class Context:
    """Routing endpoint pair: (destination, source). 4 bits each.

    Used as the ``context`` value on :class:`Send` / :class:`Recv` ops
    posted on a :class:`Router`.
    """

    destination: int
    source: int

    def header(self):
        """Encode as routing header byte: dst[3:0] | src[7:4]."""
        return (self.destination & 0xf) | ((self.source & 0xf) << 4)

    @classmethod
    def from_header(cls, byte):
        return cls(byte & 0xf, byte >> 4)


class Router(Datagram):
    """Routing dispatch over a lower `Framed` channel."""

    def __init__(self, framed: Framed, name: str = "router"):
        super().__init__(name)
        self.__framed = framed
        self.__rx_queues: dict[Context, deque] = defaultdict(deque)

    async def flush_ops(self, batch):
        # 1. Send all Send ops via the underlying framed channel.
        send_futures = []
        for op, future in batch:
            if isinstance(op, Send):
                header = bytes([op.context.header()])
                lf = self.__framed.send(header + op.data)
                send_futures.append((lf, future))

        # 2. Try to satisfy Recv ops from buffered frames; queue the rest.
        pending_recvs = []
        for op, future in batch:
            if isinstance(op, Recv):
                q = self.__rx_queues[op.context]
                if q:
                    if not future.done():
                        future.set_result((q.popleft(), op.context))
                else:
                    pending_recvs.append((op, future))

        # Await sends.
        if send_futures:
            await asyncio.gather(*[f for f, _ in send_futures])
            for lf, mf in send_futures:
                if not mf.done():
                    mf.set_result(None)

        # 3. Drain the lower framed channel until every pending recv
        #    has its frame. Mismatched frames are buffered per context.
        while pending_recvs:
            data, _ = await self.__framed.recv()
            ctx = Context.from_header(data[0])
            payload = data[1:]

            matched = False
            for i, (op, future) in enumerate(pending_recvs):
                if op.context == ctx:
                    if not future.done():
                        future.set_result((payload, ctx))
                    pending_recvs.pop(i)
                    matched = True
                    break

            if not matched:
                self.__rx_queues[ctx].append(payload)

    def route(self, local_id, remote_id):
        """Create a `Route` for a specific endpoint pair."""
        return Route(self, local_id, remote_id)


class Route(Framed):
    """Single fixed-endpoint route over a `Router`. Presents as a
    plain `Framed` channel — users don't see the routing context."""

    def __init__(self, router: Router, local_id: int, remote_id: int,
                 name: str | None = None):
        if name is None:
            name = f"route-{local_id}-{remote_id}"
        super().__init__(name)
        self.__router = router
        self.__outbound = Context(remote_id, local_id)
        self.__inbound = Context(local_id, remote_id)

    async def flush_ops(self, batch):
        lower_futures = []
        for op, future in batch:
            if isinstance(op, Send):
                lf = self.__router.post(Send(op.data, self.__outbound))
                lower_futures.append((lf, future, 'send'))
            elif isinstance(op, Recv):
                lf = self.__router.post(Recv(self.__inbound))
                lower_futures.append((lf, future, 'recv'))

        await asyncio.gather(*[f for f, _, _ in lower_futures])

        for lf, mf, kind in lower_futures:
            if kind == 'send':
                if not mf.done():
                    mf.set_result(None)
            else:
                data, _ = lf.result()
                if not mf.done():
                    mf.set_result((data, None))

    def __repr__(self):
        return f"<Route {self.__outbound}>"


class FramedEndpoint:
    """Tagged request-response transactions over a `Route`. Prepends
    an auto-incrementing 1-byte tag for correlation."""

    def __init__(self, route: Route):
        self.__route = route
        self.__tag = 0

    async def transact(self, data: bytes) -> bytes:
        tag = self.__tag
        self.__tag = (self.__tag + 1) & 0xff
        self.__route.send(bytes([tag]) + data)
        rx, _ = await self.__route.recv()
        if rx[0] != tag:
            raise RuntimeError(
                f"Tag mismatch: sent 0x{tag:02x}, got 0x{rx[0]:02x}")
        return rx[1:]
