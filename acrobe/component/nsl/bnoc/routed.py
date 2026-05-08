import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass

from ....engine import Batcher
from ....node import Node
from .framed import Framed, FrameSend, FrameRecv


@dataclass(frozen=True)
class Context:
    destination: int  # 0-15
    source: int       # 0-15

    def header(self):
        """Encode as routing header byte: dst[3:0] | src[7:4]."""
        return (self.destination & 0xf) | ((self.source & 0xf) << 4)

    @classmethod
    def from_header(cls, byte):
        return cls(byte & 0xf, byte >> 4)


class RouterSend:
    def __init__(self, data, context):
        self.data = data
        self.context = context

    def __repr__(self):
        return f"<RouterSend {len(self.data)}B ctx={self.context}>"


class RouterRecv:
    def __init__(self, context):
        self.context = context
        self.data = None

    def __repr__(self):
        return f"<RouterRecv ctx={self.context}>"


class Router(Batcher, Node):
    """Routing dispatch over Framed. Matches RTL nsl_bnoc_routed.

    Packet format: [dst(3:0) | src(7:4)] [payload...]
    """

    def __init__(self, framed: Framed, name: str = "router"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self._framed = framed
        self._rx_queues: dict[Context, deque] = defaultdict(deque)

    async def flush_ops(self, batch):
        # 1. Send all RouterSend ops
        send_futures = []
        for op, future in batch:
            if isinstance(op, RouterSend):
                header = bytes([op.context.header()])
                lf = self._framed.send(header + op.data)
                send_futures.append((lf, future))

        # 2. Collect RouterRecv ops
        pending_recvs = []
        for op, future in batch:
            if isinstance(op, RouterRecv):
                # Check buffered frames first
                q = self._rx_queues[op.context]
                if q:
                    op.data = q.popleft()
                    future.set_result(op)
                else:
                    pending_recvs.append((op, future))

        # Await sends
        if send_futures:
            await asyncio.gather(*[f for f, _ in send_futures])
            for lf, mf in send_futures:
                mf.set_result(lf.result())

        # 3. Poll lower Framed for pending recvs
        while pending_recvs:
            lf = self._framed.recv()
            result = await lf
            frame = result.data

            # Parse routing header
            ctx = Context.from_header(frame[0])
            payload = frame[1:]

            # Match to pending recv
            matched = False
            for i, (op, future) in enumerate(pending_recvs):
                if op.context == ctx:
                    op.data = payload
                    future.set_result(op)
                    pending_recvs.pop(i)
                    matched = True
                    break

            if not matched:
                # Buffer for later
                self._rx_queues[ctx].append(payload)

    def route(self, local_id, remote_id):
        """Create a Route for a specific endpoint pair."""
        return Route(self, local_id, remote_id)


class Route(Framed):
    """Single route pair. Inherits from Framed so isinstance(route, Framed) is True."""

    def __init__(self, router, local_id, remote_id, name=None):
        if name is None:
            name = f"route-{local_id}-{remote_id}"
        super().__init__(name)
        self._router = router
        self._outbound = Context(remote_id, local_id)
        self._inbound = Context(local_id, remote_id)

    async def flush_ops(self, batch):
        router_futures = []
        for op, future in batch:
            if isinstance(op, FrameSend):
                rf = self._router.post(RouterSend(op.data, self._outbound))
                router_futures.append((rf, future, 'send'))
            elif isinstance(op, FrameRecv):
                rf = self._router.post(RouterRecv(self._inbound))
                router_futures.append((rf, future, 'recv'))

        await asyncio.gather(*[f for f, _, _ in router_futures])

        for rf, mf, kind in router_futures:
            result = rf.result()
            if kind == 'recv':
                recv = FrameRecv()
                recv.data = result.data
                mf.set_result(recv)
            else:
                mf.set_result(FrameSend(result.data))

    def __repr__(self):
        return f"<Route {self._outbound}>"


class FramedEndpoint:
    """Tagged request-response transactions over a Route.
    Prepends auto-incrementing 1-byte tag for correlation.
    """

    def __init__(self, route: Route):
        self._route = route
        self._tag = 0

    async def transact(self, data: bytes) -> bytes:
        tag = self._tag
        self._tag = (self._tag + 1) & 0xff
        self._route.send(bytes([tag]) + data)
        result = await self._route.recv()
        if result.data[0] != tag:
            raise RuntimeError(
                f"Tag mismatch: sent 0x{tag:02x}, got 0x{result.data[0]:02x}")
        return result.data[1:]
