"""Client-side stand-ins for remote @wire.node Batchers.

Two layers:

* `RemoteBatcher` — generic `Batcher` whose `flush_ops` ships the
  batch over a `WireClient`. Useful when the caller doesn't care
  about the remote node's local class identity (tests, ad-hoc use).

* `make_remote_proxy(target_class, wire_client, **init_kwargs)` —
  builds an instance of a *subclass* of `target_class` whose
  `flush_ops` routes through the wire. The proxy IS-A
  `target_class`: its `child_spawn`, `db` lookup, and any other
  subclass methods work locally via MRO. A `JtagInterface` proxy
  spawns a real local `Chain()` whose subsequent bit-level ops
  flow up through the proxy → wire → remote hardware.

The dynamic-class approach is what lets a hybrid local/remote
tree work: only the @wire.node-decorated layer is transported;
everything below stays local code, posting ops through the proxy.
"""

import types
from typing import Any

from ...engine import Batcher
from .ws import WireClient


class RemoteBatcher(Batcher):
    """Generic forwarder — every batch posted goes over the wire.

    The ops in the batch are encoded by the client's Session (which
    holds the negotiated tag table). Ops the local registry doesn't
    know about will fail at encode time, with a clear error.
    """

    def __init__(self, wire_client: WireClient):
        Batcher.__init__(self)
        self._wire = wire_client

    async def flush_ops(self, batch):
        await _wire_flush_ops(self, batch)


async def _wire_flush_ops(self, batch):
    """Shared flush_ops body for RemoteBatcher and proxy classes
    built via make_remote_proxy. Reads `self._wire`."""
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
            fut.set_result(None)


def make_remote_proxy(target_class: type, wire_client: WireClient,
                      **init_kwargs: Any):
    """Build a proxy that IS-A `target_class` and routes ops over the wire.

    `init_kwargs` are forwarded to `target_class.__init__` — typically
    just `name=...` for most @wire.node-decorated Batchers.

    The returned instance:

    * inherits `target_class.child_spawn`, `db`, and every other
      subclass method, so `proxy.child_summon(...)` materializes
      genuinely local subclasses (Chain, Tap, ...) under the proxy
      that operate by posting ops through the proxy → wire;
    * overrides `flush_ops` to forward batches to `wire_client`;
    * overrides `stop` to close the wire client when the node is
      torn down.

    The proxy class is fresh on each call — different remote
    references get different class objects so isinstance() checks
    don't accidentally entangle them.
    """

    async def _proxy_stop(self):
        await target_class.stop(self)
        wire = getattr(self, "_wire", None)
        if wire is not None:
            await wire.close()
            self._wire = None

    def _exec(ns):
        ns["flush_ops"] = _wire_flush_ops
        ns["stop"] = _proxy_stop
        ns["__module__"] = target_class.__module__
        ns["__qualname__"] = f"Remote{target_class.__name__}"

    proxy_cls = types.new_class(
        f"Remote{target_class.__name__}",
        (target_class,),
        kwds={},
        exec_body=_exec,
    )
    instance = proxy_cls(**init_kwargs)
    instance._wire = wire_client
    return instance
