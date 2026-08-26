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

from ....engine import chain_future
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
        # (context, future) pairs awaiting a matching inbound frame, plus
        # a flag for whether the callback-driven drain loop is running.
        self.__pending: list = []
        self.__draining = False

    async def flush_ops(self, batch):
        # Non-blocking (see Batcher contract). Sends are lowered and
        # chained; recvs are satisfied from the per-context buffer or
        # queued and serviced by a callback-driven drain of the lower
        # channel — no awaits here.
        for op, future in batch:
            if isinstance(op, Send):
                header = bytes([op.context.header()])
                lf = self.__framed.send(header + op.data)
                chain_future(lf, future, lambda _r: None)

        for op, future in batch:
            if isinstance(op, Recv):
                q = self.__rx_queues.get(op.context)
                if q:
                    if future is not None and not future.done():
                        future.set_result((q.popleft(), op.context))
                else:
                    self.__pending.append((op.context, future))

        if self.__pending and not self.__draining:
            self.__drain()

    def __drain(self):
        self.__draining = True
        lf = self.__framed.post(Recv())
        lf.add_done_callback(self.__on_frame)

    def __on_frame(self, f):
        exc = f.exception()
        if exc is not None:
            pending, self.__pending = self.__pending, []
            self.__draining = False
            for _ctx, fut in pending:
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
            return

        data, _ = f.result()
        ctx = Context.from_header(data[0])
        payload = data[1:]

        matched = False
        for i, (pctx, fut) in enumerate(self.__pending):
            if pctx == ctx:
                if fut is not None and not fut.done():
                    fut.set_result((payload, ctx))
                self.__pending.pop(i)
                matched = True
                break
        if not matched:
            self.__rx_queues[ctx].append(payload)

        if self.__pending:
            self.__drain()
        else:
            self.__draining = False

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
        # Non-blocking: lower each op onto the router, chain the future.
        for op, future in batch:
            if isinstance(op, Send):
                lf = self.__router.post(Send(op.data, self.__outbound))
                chain_future(lf, future, lambda _r: None)
            elif isinstance(op, Recv):
                lf = self.__router.post(Recv(self.__inbound))
                chain_future(lf, future, lambda r: (r[0], None))

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
        # Non-blocking. Sends prepend the next tag and chain; recvs pop
        # the matching tag and validate it in the resolution callback.
        # Lower recvs resolve in order, so the callbacks pop tags in the
        # same order they were pushed.
        for op, future in batch:
            if isinstance(op, Send):
                tag = self.__send_tag
                self.__send_tag = (self.__send_tag + 1) & 0xff
                self.__pending_tags.append(tag)
                lf = self.__channel.post(Send(bytes([tag]) + op.data, op.context))
                chain_future(lf, future, lambda _r: None)
            elif isinstance(op, Recv):
                lf = self.__channel.post(Recv(op.context))
                lf.add_done_callback(
                    lambda f, fut=future: self.__on_recv(fut, f))

    def __on_recv(self, future, f):
        exc = f.exception()
        if exc is not None:
            if future is not None and not future.done():
                future.set_exception(exc)
            return
        data, ctx = f.result()
        expected = self.__pending_tags.popleft() if self.__pending_tags else None
        if future is None or future.done():
            return
        if not data:
            future.set_exception(RuntimeError(
                "FramedEndpoint: empty frame on recv"))
            return
        if expected is not None and data[0] != expected:
            future.set_exception(RuntimeError(
                f"FramedEndpoint tag mismatch: expected 0x{expected:02x}, "
                f"got 0x{data[0]:02x}"))
            return
        future.set_result((data[1:], ctx))
