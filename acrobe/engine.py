import asyncio
from typing import Any


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
        self._pending: list[tuple[Any, asyncio.Future]] = []
        self._flush_task: asyncio.Task | None = None

    def post(self, op) -> asyncio.Future:
        """
        Enqueue an operation synchronously. Returns a Future that resolves
        when the operation completes. The Future's result is the op itself
        (with any results populated on it).
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending.append((op, future))
        self._ensure_flush()
        return future

    def _ensure_flush(self):
        """Schedule a flush task if one isn't already running."""
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._run_flush())

    async def _run_flush(self):
        """
        Drain and process pending ops. Loops because flush_ops() may await
        lower layers, during which new ops can be posted.
        """
        while self._pending:
            batch = self._pending
            self._pending = []
            try:
                await self.flush_ops(batch)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                for _, future in batch:
                    if not future.done():
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

        If this method raises, _run_flush will reject all unresolved futures
        with the exception.
        """
        raise NotImplementedError
