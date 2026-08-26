"""Tests for the acrobe.event bus (Slice 1)."""

import asyncio
import logging

import pytest

from acrobe.event import (
    Action, Event, EventBus, Phase, Subscription,
    get_bus, reset_for_tests,
)


@pytest.fixture(autouse=True)
def __isolated_bus():
    """Each test gets a fresh global bus."""
    reset_for_tests()
    yield
    reset_for_tests()


def make_event(source="a/b", action="program", phase=Phase.PRE,
               **properties) -> Event:
    return Event(source=source, action=action, phase=phase,
                 properties=properties)


class TestEvent:
    def test_default_phase_is_none(self):
        evt = Event(source="a", action="changed")
        assert evt.phase is None

    def test_source_is_exact(self):
        evt = make_event(source="a/b")
        assert evt.source_is("a/b") is True
        assert evt.source_is("a") is False

    def test_source_under_self(self):
        evt = make_event(source="a/b")
        assert evt.source_under("a/b") is True

    def test_source_under_ancestor(self):
        evt = make_event(source="a/b/c")
        assert evt.source_under("a/b") is True

    def test_source_under_unrelated(self):
        evt = make_event(source="a/c")
        assert evt.source_under("a/b") is False

    def test_source_under_root(self):
        evt = make_event(source="anything/at/all")
        assert evt.source_under("") is True


class TestSubscriptionLifecycle:
    @pytest.mark.asyncio
    async def test_subscribe_then_cancel_no_call(self):
        bus = get_bus()
        calls = []
        sub = bus.subscribe(lambda e: calls.append(e))
        sub.cancel()
        await bus.emit(make_event())
        assert calls == []

    @pytest.mark.asyncio
    async def test_cancel_is_idempotent(self):
        bus = get_bus()
        sub = bus.subscribe(lambda e: None)
        sub.cancel()
        sub.cancel()  # should not raise

    @pytest.mark.asyncio
    async def test_subscription_context_manager(self):
        bus = get_bus()
        calls = []
        with bus.subscribe(lambda e: calls.append(e)) as sub:
            assert isinstance(sub, Subscription)
            await bus.emit(make_event())
        await bus.emit(make_event())
        assert len(calls) == 1


class TestActionFilter:
    @pytest.mark.asyncio
    async def test_no_action_filter_matches_all(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.action))
        await bus.emit(make_event(action="program"))
        await bus.emit(make_event(action="reset"))
        assert calls == ["program", "reset"]

    @pytest.mark.asyncio
    async def test_action_filter_single(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.action), action="program")
        await bus.emit(make_event(action="program"))
        await bus.emit(make_event(action="reset"))
        assert calls == ["program"]

    @pytest.mark.asyncio
    async def test_action_filter_iterable(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.action),
                      action=("program", "reset"))
        await bus.emit(make_event(action="program"))
        await bus.emit(make_event(action="mutate"))
        await bus.emit(make_event(action="reset"))
        assert calls == ["program", "reset"]


class TestPhaseFilter:
    @pytest.mark.asyncio
    async def test_no_phase_filter_matches_all_including_none(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.phase))
        await bus.emit(make_event(phase=Phase.PRE))
        await bus.emit(make_event(phase=Phase.POST))
        await bus.emit(make_event(phase=None))
        assert calls == [Phase.PRE, Phase.POST, None]

    @pytest.mark.asyncio
    async def test_phase_post_excludes_none(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.phase), phase=Phase.POST)
        await bus.emit(make_event(phase=Phase.PRE))
        await bus.emit(make_event(phase=Phase.POST))
        await bus.emit(make_event(phase=None))
        assert calls == [Phase.POST]

    @pytest.mark.asyncio
    async def test_phase_filter_explicit_none(self):
        # Pass [None] explicitly to match only phaseless events.
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.phase), phase=(None,))
        await bus.emit(make_event(phase=Phase.POST))
        await bus.emit(make_event(phase=None))
        assert calls == [None]


class TestSourceFilter:
    @pytest.mark.asyncio
    async def test_source_exact_match(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.source),
                      source="a/b", source_match="exact")
        await bus.emit(make_event(source="a/b"))
        await bus.emit(make_event(source="a/b/c"))
        await bus.emit(make_event(source="a"))
        assert calls == ["a/b"]

    @pytest.mark.asyncio
    async def test_source_subtree_match(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.source),
                      source="a/b", source_match="subtree")
        await bus.emit(make_event(source="a/b"))
        await bus.emit(make_event(source="a/b/c"))
        await bus.emit(make_event(source="a/b/c/d"))
        await bus.emit(make_event(source="a/c"))
        await bus.emit(make_event(source="a/bb"))
        assert calls == ["a/b", "a/b/c", "a/b/c/d"]

    def test_source_match_validation(self):
        bus = get_bus()
        with pytest.raises(ValueError):
            bus.subscribe(lambda e: None, source="x", source_match="weird")


class TestPredicateFilter:
    @pytest.mark.asyncio
    async def test_predicate(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.properties.get("n")),
                      predicate=lambda e: e.properties.get("n", 0) > 5)
        await bus.emit(make_event(n=3))
        await bus.emit(make_event(n=7))
        await bus.emit(make_event(n=10))
        assert calls == [7, 10]

    @pytest.mark.asyncio
    async def test_predicate_runs_after_other_filters(self):
        # Predicate isn't even called for events that don't pass
        # the structured filters.
        bus = get_bus()
        called_pred = []

        def pred(evt):
            called_pred.append(evt.action)
            return True

        bus.subscribe(lambda e: None, action="program", predicate=pred)
        await bus.emit(make_event(action="reset"))
        await bus.emit(make_event(action="program"))
        assert called_pred == ["program"]


class TestHandlerDispatch:
    @pytest.mark.asyncio
    async def test_sync_handler_called_inline(self):
        bus = get_bus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.source))
        await bus.emit(make_event(source="x"))
        assert calls == ["x"]

    @pytest.mark.asyncio
    async def test_async_handler_awaited(self):
        bus = get_bus()
        calls = []

        async def handler(evt):
            await asyncio.sleep(0)
            calls.append(evt.source)

        bus.subscribe(handler)
        await bus.emit(make_event(source="x"))
        assert calls == ["x"]

    @pytest.mark.asyncio
    async def test_multiple_async_handlers_concurrent(self):
        bus = get_bus()
        order = []

        async def slow(evt):
            await asyncio.sleep(0.05)
            order.append("slow")

        async def fast(evt):
            order.append("fast")

        bus.subscribe(slow)
        bus.subscribe(fast)
        await bus.emit(make_event())
        # `fast` ran first because `slow` awaited; both completed
        # before emit returned.
        assert order == ["fast", "slow"]

    @pytest.mark.asyncio
    async def test_emit_returns_after_all_handlers_done(self):
        bus = get_bus()
        done = asyncio.Event()

        async def handler(evt):
            await asyncio.sleep(0.05)
            done.set()

        bus.subscribe(handler)
        await bus.emit(make_event())
        assert done.is_set()

    @pytest.mark.asyncio
    async def test_no_subscribers_emit_returns_immediately(self):
        bus = get_bus()
        await bus.emit(make_event())  # no exception


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_sync_handler_exception_caught_and_logged(self, caplog):
        bus = get_bus()
        calls = []

        def bad(evt):
            raise RuntimeError("boom")

        def ok(evt):
            calls.append(evt.action)

        bus.subscribe(bad)
        bus.subscribe(ok)
        with caplog.at_level(logging.WARNING, logger="acrobe.event"):
            await bus.emit(make_event(action="program"))
        # Good subscriber still ran.
        assert calls == ["program"]
        # Bad handler's exception got logged.
        assert any("boom" in r.message or "RuntimeError" in r.message
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_async_handler_exception_caught_and_logged(self, caplog):
        bus = get_bus()

        async def bad(evt):
            raise RuntimeError("async boom")

        async def ok(evt):
            pass

        bus.subscribe(bad)
        bus.subscribe(ok)
        with caplog.at_level(logging.WARNING, logger="acrobe.event"):
            await bus.emit(make_event())
        assert any("async boom" in r.message or "RuntimeError" in r.message
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate_to_publisher(self):
        bus = get_bus()

        def bad(evt):
            raise RuntimeError("nope")

        bus.subscribe(bad)
        # If emit propagated, this would raise.
        await bus.emit(make_event())


class TestCrossEmit:
    @pytest.mark.asyncio
    async def test_concurrent_emits_independent(self):
        bus = get_bus()
        seen_a = []
        seen_b = []

        bus.subscribe(lambda e: seen_a.append(e.action), action="a")
        bus.subscribe(lambda e: seen_b.append(e.action), action="b")

        await asyncio.gather(
            bus.emit(make_event(action="a")),
            bus.emit(make_event(action="b")),
        )

        assert seen_a == ["a"]
        assert seen_b == ["b"]

    @pytest.mark.asyncio
    async def test_subscribe_during_emit(self):
        # Adding a subscription during an emit must not affect
        # that emit (snapshot semantics), but should be visible
        # to the next emit.
        bus = get_bus()
        added_during: list = []

        def adder(evt):
            sub = bus.subscribe(lambda e: added_during.append(e.action))
            # Keep alive
            adder.sub = sub

        bus.subscribe(adder)
        await bus.emit(make_event(action="first"))
        assert added_during == []  # the new sub didn't fire on its own creation
        await bus.emit(make_event(action="second"))
        assert added_during == ["second"]

    @pytest.mark.asyncio
    async def test_cancel_during_emit(self):
        bus = get_bus()
        calls = []

        sub_holder = {}

        def self_cancelling(evt):
            sub_holder["sub"].cancel()
            calls.append(evt.action)

        sub_holder["sub"] = bus.subscribe(self_cancelling)
        await bus.emit(make_event(action="one"))
        await bus.emit(make_event(action="two"))
        # First emit ran (snapshot), but second didn't (cancelled).
        assert calls == ["one"]


class TestActionsModule:
    def test_canonical_action_constants(self):
        assert Action.PROGRAM == "program"
        assert Action.CHANGED == "changed"
        assert Action.ATTACH == "attach"
        assert Action.MOVED == "moved"


class TestGlobalBus:
    @pytest.mark.asyncio
    async def test_get_bus_singleton_within_session(self):
        a = get_bus()
        b = get_bus()
        assert a is b

    @pytest.mark.asyncio
    async def test_reset_for_tests_recreates(self):
        a = get_bus()
        reset_for_tests()
        b = get_bus()
        assert a is not b

    @pytest.mark.asyncio
    async def test_direct_eventbus_instance(self):
        # Anyone can instantiate EventBus directly (e.g. tests
        # wanting fully local isolation).
        bus = EventBus()
        calls = []
        bus.subscribe(lambda e: calls.append(e.action))
        await bus.emit(make_event(action="x"))
        assert calls == ["x"]
