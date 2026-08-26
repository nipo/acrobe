import asyncio
from typing import Any, Callable
from collections import deque
from .log import PROTOCOL


def chain_future(source: asyncio.Future,
                 target: asyncio.Future | None,
                 transform: Callable[[Any], Any] | None = None) -> None:
    """Resolve `target` from `source` when `source` completes.

    This is the cornerstone of the non-blocking ``flush_ops`` contract
    (see :class:`Batcher`): a layer lowers its ops, ``post()``s them to
    the layer below, and calls ``chain_future`` to wire each lower-layer
    future to the matching upper-layer future — then returns *without
    awaiting*. When `source` resolves, `target` is resolved with
    ``transform(source.result())`` (or the result unchanged when
    `transform` is None); exceptions propagate. `target` may be None
    when the upper op had no waiter (``post_no_wait``).

    Because the callback runs *after* ``flush_ops`` has returned, it must
    resolve/reject its future itself — the Batcher's flush-time
    try/except does not cover it. This helper does that; hand-written
    callbacks must too.
    """
    def _done(src: asyncio.Future) -> None:
        if target is None or target.done():
            return
        exc = src.exception()
        if exc is not None:
            target.set_exception(exc)
            return
        try:
            value = src.result()
            target.set_result(value if transform is None else transform(value))
        except BaseException as e:   # noqa: BLE001 — forwarded to the future
            target.set_exception(e)
    source.add_done_callback(_done)


class Batcher:
    """
    Generic async batching engine.

    Subclass and implement flush_ops() to process batches of operations.
    Operations are posted synchronously via post(), which returns a Future.
    A flush task is scheduled idempotently via asyncio.create_task.
    Multiple post() calls within the same synchronous block batch together.

    Fire-and-forget: operations execute even if Futures are never awaited.

    ## flush_ops MUST NOT await IO

    ``flush_ops`` runs while the Batcher holds its flush lock, so a batch
    that blocks inside ``flush_ops`` stalls *every following batch on the
    same node* — they cannot flush until it returns. Worse, a layer that
    ``await``s each lowered op in turn serialises the wire: it can hand a
    huge write to the transport with no read pending behind it, which
    deadlocks any full-duplex device that must have its response drained
    while it consumes the request.

    So the contract is: **lower, submit-all, chain, return.** A
    ``flush_ops`` implementation must

    1. translate every op in the batch into lower-layer ops,
    2. ``post()`` them all to the layer below (no ``await`` between them),
    3. wire each lower-layer future back to its upper-layer future with
       :func:`chain_future` (or an ``add_done_callback`` for stateful
       cases), and
    4. return immediately.

    It must **never** ``await`` an IO future inside ``flush_ops`` and must
    **never** process the ops in strict order with a blocking read in the
    middle. Any real waiting (an incremental read state machine, a
    demux loop) belongs in a background task or a callback chain that
    outlives the ``flush_ops`` call — never in the call itself.

    ``Ft245SyncPipe``, ``JtagMpsse``, and the ``nsl.bnoc`` datagram
    layers (``Chunked``, ``Router``, ``FramedEndpoint``, ``Committed``)
    are the reference implementations.
    """

    def __init__(self):
        self.__pending = deque()
        self.__flush_task: asyncio.Task | None = None
        self.__lock = asyncio.Lock()

    def post(self, op) -> asyncio.Future:
        """
        Enqueue an operation synchronously. Returns a Future that resolves
        when the operation completes. The Future's result is the op itself
        (with any results populated on it).
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.__pending.append((op, future))
        self.__ensure_flush()
        return future

    def post_no_wait(self, op) -> None:
        """Enqueue an op without creating a Future for it.

        Use when an upstream future is already anchored on a *different*
        op in the same batch — typical for the bottom of a batched
        callback chain, where the layer reads each op's side-effects
        (e.g. ``op.data``) directly out of the recorded list and only
        needs ONE future on the last op to fire its batched callback.
        Skips a per-op ``loop.create_future()`` plus the per-op
        set_result on resolution; both are nontrivial in tight inner
        loops with thousands of ops per chunk.
        """
        self.__pending.append((op, None))
        self.__ensure_flush()

    def __ensure_flush(self):
        """Schedule a flush task if one isn't already running."""
        if self.__flush_task is None or self.__flush_task.done():
            self.__flush_task = asyncio.create_task(self.__run_flush())

    async def __run_flush(self):
        """
        Drain and process pending ops. Decouple from future
        iterations (they will spawn another task.
        """
        async with self.__lock:
            self.__pending, batch = deque(), list(self.__pending)
            self.__flush_task = None

            try:
                # Skip building the per-op list when PROTOCOL logging
                # is off — for the deepest layers this list spans
                # thousands of items per batch and the formatting cost
                # dominates the actual flush work.
                if self.logger.isEnabledFor(PROTOCOL):
                    self.logger.protocol(
                        "Batching %s", [top for top, _ in batch])
                await self.flush_ops(batch)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                for _, future in batch:
                    if future is not None and not future.done():
                        future.set_exception(exc)

    async def flush_ops(self, batch: list[tuple[Any, asyncio.Future]]):
        """
        Process a batch of (op, future) pairs.

        Subclasses MUST resolve or reject every future in the batch.
        Typical implementation:
          1. Translate ops into lower-layer format
          2. Post to lower layer, collect lower futures
          3. Await lower futures
          4. Extract results, resolve own futures

        If this method raises, the flush loop will reject all unresolved
        futures with the exception.
        """
        raise NotImplementedError


class BackgroundLowering:
    """Run a batch against a plain-coroutine backend.

    Some address spaces sit on a command-oriented backend — a USB
    vendor command set, an SPI command sequence with busy polling —
    rather than on another Batcher, so their lowering has to await.
    ``dispatch(batch)`` hands the batch to a background task and
    returns immediately, keeping ``flush_ops`` free of IO; a lock
    holds batches in submission order.

    Subclasses implement :meth:`run_ops`, which may await freely and
    must resolve every non-None future.
    """

    __lock: asyncio.Lock | None = None

    def dispatch(self, batch) -> None:
        if self.__lock is None:
            self.__lock = asyncio.Lock()
        asyncio.ensure_future(self.__drain(batch, self.__lock))

    async def __drain(self, batch, lock: asyncio.Lock) -> None:
        async with lock:
            try:
                await self.run_ops(batch)
            except Exception as exc:
                self.__reject(batch, exc)
            else:
                self.__reject(batch, None)

    def __reject(self, batch, exc: BaseException | None) -> None:
        for op, future in batch:
            if future is None or future.done():
                continue
            future.set_exception(exc if exc is not None else RuntimeError(
                f"{type(self).__name__} left {op!r} unresolved"))

    async def run_ops(self, batch):
        raise NotImplementedError
