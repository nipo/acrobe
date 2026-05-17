import asyncio
from typing import Any
from collections import deque
from .log import PROTOCOL


class Batcher:
    """
    Generic async batching engine.

    Subclass and implement flush_ops() to process batches of operations.
    Operations are posted synchronously via post(), which returns a Future.
    A flush task is scheduled idempotently via asyncio.create_task.
    Multiple post() calls within the same synchronous block batch together.

    Fire-and-forget: operations execute even if Futures are never awaited.
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
