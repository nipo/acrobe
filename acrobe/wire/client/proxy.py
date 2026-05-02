"""RemoteBatcher — client-side stand-in for a remote @wire.node Batcher.

Subclasses `Batcher` so callers can use the same `post(op) → Future`
API as the local node. `flush_ops` ships the batch over the wire
client and distributes Response.results / Response.errors back to
the per-op futures.

For domain-specific convenience methods (e.g. `JtagInterface.run()`)
the proxy is typically subclassed alongside the local node class —
the convenience methods construct an op and call `self.post(op)`
exactly as they do locally. This is wired up in phase 4b when the
JTAG classes get their `@wire.node` decoration.
"""

from ...engine import Batcher
from .ws import WireClient


class RemoteBatcher(Batcher):
    """Forwards every batch to a `WireClient`.

    The ops in the batch are encoded by the client's Session (which
    holds the negotiated tag table). Ops the local registry doesn't
    know about will fail at encode time, with a clear error.
    """

    def __init__(self, wire_client: WireClient):
        Batcher.__init__(self)
        self._wire = wire_client

    async def flush_ops(self, batch):
        ops = [op for op, _ in batch]
        try:
            response = await self._wire.send_batch(ops)
        except Exception as exc:
            for _, fut in batch:
                if not fut.done():
                    fut.set_exception(exc)
            return

        for idx, (op, fut) in enumerate(batch):
            if fut.done():
                continue
            if idx in response.errors:
                fut.set_exception(response.errors[idx])
            elif idx in response.results:
                fut.set_result(response.results[idx])
            else:
                # Op had no future-producing semantics on the server.
                fut.set_result(None)
