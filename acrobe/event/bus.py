"""EventBus, Subscription, and the process-global accessor.

The bus is a fan-out dispatcher: publishers post Events, the bus
finds matching subscriptions and runs their handlers concurrently.
Per-handler exceptions are caught and logged; they never
propagate to the publisher. The bus knows nothing about the Node
tree — subscriptions match on `source` strings only.

The bus is global to the process. `get_bus()` returns the
singleton; `reset_for_tests()` recreates it so tests get
isolation.
"""

import asyncio
import inspect
import logging
from collections.abc import Callable, Iterable
from typing import Any, Literal

from ..node import Path
from .event import Event


_LOG = logging.getLogger("acrobe.event")


class Subscription:
    """Handle for one subscription. Sync context-manager form for
    scoped use; `cancel()` for explicit release.

    Holds back-references to the bus and the filter so cancel()
    can deregister atomically. Held strongly by the caller; the
    bus holds a strong reference too until cancelled."""

    def __init__(self,
                 bus: "EventBus",
                 handler: Callable[[Event], Any],
                 actions: frozenset[str] | None,
                 phases: frozenset[str | None] | None,
                 source: str | None,
                 source_match: str,
                 predicate: Callable[[Event], bool] | None):
        self.__bus = bus
        self.__handler = handler
        self.__actions = actions
        self.__phases = phases
        self.__source = source
        self.__source_match = source_match
        self.__predicate = predicate
        self.__cancelled = False

    def __repr__(self):
        bits = []
        if self.__actions is not None:
            bits.append(f"action={sorted(self.__actions)!r}")
        if self.__phases is not None:
            bits.append(f"phase={sorted(str(p) for p in self.__phases)!r}")
        if self.__source is not None:
            bits.append(f"source={self.__source!r}/{self.__source_match}")
        if self.__predicate is not None:
            bits.append("predicate=<fn>")
        return f"<Subscription {self.__handler.__qualname__} {' '.join(bits)}>"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cancel()
        return False

    def cancel(self) -> None:
        """Idempotent."""
        if self.__cancelled:
            return
        self.__cancelled = True
        self.__bus._drop(self)

    @property
    def cancelled(self) -> bool:
        return self.__cancelled

    @property
    def handler(self):
        return self.__handler

    def matches(self, event: Event) -> bool:
        if self.__cancelled:
            return False
        if self.__actions is not None and event.action not in self.__actions:
            return False
        if self.__phases is not None and event.phase not in self.__phases:
            return False
        if self.__source is not None:
            if self.__source_match == "exact":
                if event.source != self.__source:
                    return False
            else:  # subtree
                if not Path.is_descendant_or_self(event.source, self.__source):
                    return False
        if self.__predicate is not None and not self.__predicate(event):
            return False
        return True


class EventBus:
    """Process-global event dispatcher.

    `emit(event)` is async: it awaits all matching handlers
    (sync ones called inline, async ones gathered concurrently)
    and returns once every handler has completed.

    Per-handler exceptions are caught and logged at WARNING.
    They never propagate to the publisher — the bus is a
    notification channel, not an RPC. Subscribers that want
    retry semantics implement them inside their handler.
    """

    def __init__(self):
        self.__subscriptions: list[Subscription] = []

    def subscribe(self,
                  handler: Callable[[Event], Any],
                  *,
                  action: str | Iterable[str] | None = None,
                  phase: str | Iterable[str] | None = None,
                  source: str | None = None,
                  source_match: Literal["exact", "subtree"] = "exact",
                  predicate: Callable[[Event], bool] | None = None,
                  ) -> Subscription:
        """Register `handler` for events matching the filters.

        Every filter ANDs with the others. None means "no
        constraint on this axis". For `phase`, pass `None` (the
        default) to match every phase including phaseless
        events; pass a sequence containing `None` explicitly to
        match only phaseless events.

        Returns a Subscription handle. Call `.cancel()` or use
        as a sync context manager to release.
        """
        actions = self.__as_frozen_str(action)
        phases = self.__as_frozen_phase(phase)
        if source_match not in ("exact", "subtree"):
            raise ValueError(
                f"source_match must be 'exact' or 'subtree', got {source_match!r}")
        sub = Subscription(self, handler,
                           actions, phases,
                           source, source_match, predicate)
        self.__subscriptions.append(sub)
        return sub

    async def emit(self, event: Event) -> None:
        """Dispatch `event` to every matching subscription.

        Awaits all handlers; returns once they're done.
        """
        # Snapshot at dispatch time so subscribe/cancel during
        # the emit don't mutate the iteration.
        candidates = [s for s in list(self.__subscriptions)
                      if s.matches(event)]
        if not candidates:
            return
        pending: list[asyncio.Future] = []
        for sub in candidates:
            try:
                result = sub.handler(event)
            except BaseException as exc:
                self.__log_failure(sub, event, exc)
                continue
            if inspect.isawaitable(result):
                pending.append(self.__wrap_async(sub, event, result))
        if pending:
            await asyncio.gather(*pending, return_exceptions=False)

    async def __wrap_async(self, sub, event, awaitable):
        """Await `awaitable` for `sub`; catch+log any exception so
        the gather() at emit-level doesn't see it."""
        try:
            await awaitable
        except BaseException as exc:
            self.__log_failure(sub, event, exc)

    def __log_failure(self, sub: Subscription, event: Event, exc: BaseException) -> None:
        # Source path appended as logger-name suffix so per-tree
        # silencing (`--silent-re acrobe.event.proby-9`) works.
        logger_name = "acrobe.event"
        if event.source:
            logger_name = f"{logger_name}.{event.source.replace('/', '.')}"
        logger = logging.getLogger(logger_name)
        logger.warning(
            "handler %r failed for action=%r phase=%r source=%r: %s: %s",
            sub, event.action, event.phase, event.source,
            type(exc).__name__, exc)
        logger.debug("traceback for failed handler", exc_info=exc)

    def _drop(self, sub: Subscription) -> None:
        """Internal — called from Subscription.cancel."""
        try:
            self.__subscriptions.remove(sub)
        except ValueError:
            pass

    @staticmethod
    def __as_frozen_str(value) -> frozenset[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return frozenset((value,))
        return frozenset(value)

    @staticmethod
    def __as_frozen_phase(value) -> frozenset[str | None] | None:
        # Phase accepts None as a real filter value (matches
        # phase-less events). The outer None still means "no
        # filter at all".
        if value is None:
            return None
        if isinstance(value, str):
            return frozenset((value,))
        return frozenset(value)


__bus: EventBus | None = None


def get_bus() -> EventBus:
    """Return the process-global bus, creating it on first call."""
    global __bus
    if __bus is None:
        __bus = EventBus()
    return __bus


def reset_for_tests() -> None:
    """Drop the global bus so the next get_bus() call creates a
    fresh one. Tests call this in setup/teardown for isolation."""
    global __bus
    __bus = None
