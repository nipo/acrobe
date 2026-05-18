"""NSL bnoc routed channel.

Matches RTL ``nsl_bnoc.routed``: a single datagram channel multiplexes
several point-to-point endpoints. Each frame on the wire carries a
one-byte routing header ``[dst(3:0) | src(7:4)]`` followed by the
payload.

`Router` is the multiplexer (a `Datagram` with routing context).
`Route` is a sugar wrapper that pins one (local, remote) endpoint
pair on a `Router` and exposes itself as a plain `Datagram` (no
routing visible to its users).

`FramedEndpoint` wraps a `Route` (or any `Datagram`) and adds an
auto-incrementing 1-byte tag prepended to every Send and validated
on every Recv — a transactional request/response shim.
"""

from collections import defaultdict, deque
from dataclasses import dataclass

import asyncio

from ....protocol.datagram import Datagram, Send, Recv


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
    """Routing dispatch over a lower `Datagram` channel."""

    def __init__(self, channel: Datagram, name: str = "router"):
        super().__init__(name)
        self.__framed = channel
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


class Route(Datagram):
    """Single fixed-endpoint route over a `Router`. Presents as a
    plain `Datagram` — users don't see the routing context."""

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


class FramedEndpoint(Datagram):
    """Auto-tagged request/response wrapper over a `Datagram` backend.

    Each Send prepends an 8-bit incrementing tag byte; each Recv pops
    the leading byte and validates that it matches the tag of the
    most recently posted Send. Frames whose tag doesn't match the
    expected one are treated as a hard protocol error.

    Users see a plain `Datagram` with one byte less of payload.
    """

    def __init__(self, channel: Datagram, name: str = "endpoint"):
        super().__init__(name)
        self.__channel = channel
        self.__send_tag = 0
        self.__pending_tags: deque[int] = deque()

    async def flush_ops(self, batch):
        lower = []
        for op, future in batch:
            if isinstance(op, Send):
                tag = self.__send_tag
                self.__send_tag = (self.__send_tag + 1) & 0xff
                self.__pending_tags.append(tag)
                lf = self.__channel.post(Send(bytes([tag]) + op.data, op.context))
                lower.append((lf, future, "send", tag))
            elif isinstance(op, Recv):
                lf = self.__channel.post(Recv(op.context))
                lower.append((lf, future, "recv", None))

        await asyncio.gather(*[f for f, _, _, _ in lower])

        for lf, mf, kind, tag in lower:
            if mf is None or mf.done():
                continue
            if kind == "send":
                mf.set_result(None)
                continue
            data, ctx = lf.result()
            expected = self.__pending_tags.popleft() if self.__pending_tags else None
            if not data:
                mf.set_exception(RuntimeError(
                    "FramedEndpoint: empty frame on recv"))
                continue
            if expected is not None and data[0] != expected:
                mf.set_exception(RuntimeError(
                    f"FramedEndpoint tag mismatch: expected 0x{expected:02x}, "
                    f"got 0x{data[0]:02x}"))
                continue
            mf.set_result((data[1:], ctx))
