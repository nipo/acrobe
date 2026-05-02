"""Lifecycle registry contract tests."""

import pytest

from acrobe.lifecycle import Lifecycle


@pytest.mark.asyncio
async def test_register_and_shutdown_drains_in_lifo_order():
    cycle = Lifecycle()
    log = []

    async def first():
        log.append("first")

    async def second():
        log.append("second")

    cycle.register(first)
    cycle.register(second)

    await cycle.shutdown()
    assert log == ["second", "first"]
    assert cycle.pending() == 0


@pytest.mark.asyncio
async def test_cancel_removes_callback_silently():
    cycle = Lifecycle()
    log = []

    async def stays():
        log.append("stays")

    async def cancelled():
        log.append("cancelled")

    cycle.register(cancelled)
    cycle.register(stays)
    cycle.cancel(cancelled)

    await cycle.shutdown()
    assert log == ["stays"]


@pytest.mark.asyncio
async def test_cancel_unknown_callback_is_noop():
    cycle = Lifecycle()

    async def never_registered():
        pass

    cycle.cancel(never_registered)  # must not raise


@pytest.mark.asyncio
async def test_callback_exception_is_logged_not_raised():
    cycle = Lifecycle()
    log = []

    async def angry():
        raise RuntimeError("boom")

    async def calm():
        log.append("calm")

    cycle.register(calm)
    cycle.register(angry)

    await cycle.shutdown()  # must not raise
    assert log == ["calm"]


@pytest.mark.asyncio
async def test_shutdown_is_idempotent():
    cycle = Lifecycle()

    async def cleanup():
        pass

    cycle.register(cleanup)
    await cycle.shutdown()
    await cycle.shutdown()  # second call drains an empty queue
    assert cycle.pending() == 0


@pytest.mark.asyncio
async def test_register_during_shutdown_does_not_loop():
    """A callback that adds another cleanup should not trigger
    in this same shutdown — the snapshot is taken upfront."""
    cycle = Lifecycle()
    log = []

    async def added_later():
        log.append("late")

    async def initial():
        log.append("initial")
        cycle.register(added_later)

    cycle.register(initial)
    await cycle.shutdown()
    assert log == ["initial"]
    assert cycle.pending() == 1


@pytest.mark.asyncio
async def test_default_singleton_accessor():
    from acrobe.lifecycle import lifecycle, on_shutdown, cancel_shutdown

    log = []

    async def cb():
        log.append("ran")

    on_shutdown(cb)
    assert lifecycle().pending() >= 1

    cancel_shutdown(cb)
    # cb is removed; calling default shutdown won't run it
    from acrobe.lifecycle import shutdown
    await shutdown()
    assert log == []
