"""Tests for Node-side event integration (Slice 2).

Covers Node.emit, Node.subscribe (subtree default + auto-cancel
on stop_tree), Node.event_emitter (pre/post/exception/success),
Node.notified decorator, Notifier.progress.
"""

import asyncio

import pytest

from acrobe.event import Event, Notifier, Phase, get_bus, reset_for_tests
from acrobe.node import Node


@pytest.fixture(autouse=True)
def reset_bus():
    reset_for_tests()
    yield
    reset_for_tests()


def make_tree():
    """Three-level tree: root -> a -> b."""
    root = Node("root")
    a = Node("a")
    b = Node("b")
    root.child_add(a)
    a.child_add(b)
    return root, a, b


class TestNodeEmit:
    @pytest.mark.asyncio
    async def test_emit_uses_node_path_as_source(self):
        root, a, b = make_tree()
        seen: list[Event] = []
        get_bus().subscribe(lambda e: seen.append(e))
        await b.emit("ping", phase=Phase.POST, n=3)
        assert len(seen) == 1
        assert seen[0].source == "root/a/b"
        assert seen[0].action == "ping"
        assert seen[0].phase == Phase.POST
        assert seen[0].properties == {"n": 3}

    @pytest.mark.asyncio
    async def test_emit_phase_defaults_to_none(self):
        root, a, b = make_tree()
        seen: list[Event] = []
        get_bus().subscribe(lambda e: seen.append(e))
        await b.emit("changed")
        assert seen[0].phase is None

    @pytest.mark.asyncio
    async def test_emit_carries_kwargs_as_properties(self):
        root, a, b = make_tree()
        seen: list[Event] = []
        get_bus().subscribe(lambda e: seen.append(e))
        await b.emit("custom", key1="v1", key2=42)
        assert seen[0].properties == {"key1": "v1", "key2": 42}


class TestNodeSubscribe:
    @pytest.mark.asyncio
    async def test_default_subtree_match(self):
        # Subscribing on `a` sees events from `a` itself and from `b`.
        root, a, b = make_tree()
        seen = []
        a.subscribe(lambda e: seen.append(e.source))
        await a.emit("x")
        await b.emit("y")
        await root.emit("z")  # root is outside the subtree
        assert seen == ["root/a", "root/a/b"]

    @pytest.mark.asyncio
    async def test_exact_match_when_requested(self):
        root, a, b = make_tree()
        seen = []
        a.subscribe(lambda e: seen.append(e.source), source_match="exact")
        await a.emit("x")
        await b.emit("y")
        assert seen == ["root/a"]

    @pytest.mark.asyncio
    async def test_subscription_auto_cancel_on_stop_tree(self):
        # Subscribe through a's `subscribe`; stop a's subtree;
        # subsequent emits matching the path don't fire.
        # Filter to test-specific actions to avoid picking up the
        # auto-wired stop/detach lifecycle events.
        root, a, b = make_tree()
        await root.start_tree()
        seen = []
        a.subscribe(lambda e: seen.append(e.source),
                    action=("first", "second"))
        await b.emit("first")
        assert seen == ["root/a/b"]
        await a.stop_tree()
        await b.emit("second")  # b still exists as a Python object
        assert seen == ["root/a/b"]

    @pytest.mark.asyncio
    async def test_subscription_not_held_after_stop_tree(self):
        # The Subscription object reports cancelled after stop_tree.
        root, a, b = make_tree()
        await root.start_tree()
        sub = a.subscribe(lambda e: None)
        assert sub.cancelled is False
        await a.stop_tree()
        assert sub.cancelled is True

    @pytest.mark.asyncio
    async def test_direct_bus_subscription_survives_stop_tree(self):
        # Bus-level subscriptions (not tracked on any Node) are
        # not affected by a Node's stop_tree.
        root, a, b = make_tree()
        await root.start_tree()
        seen = []
        get_bus().subscribe(lambda e: seen.append(e.action),
                            action=("before", "after"),
                            source="root/a/b", source_match="subtree")
        await b.emit("before")
        await a.stop_tree()
        await b.emit("after")
        assert seen == ["before", "after"]


class TestEventEmitter:
    @pytest.mark.asyncio
    async def test_pre_and_post_fire_around_block(self):
        root, a, b = make_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            source="root/a", source_match="subtree")
        async with b.event_emitter("program", target="root/a"):
            pass
        actions_phases = [(e.action, e.phase) for e in events]
        assert actions_phases == [
            ("program", Phase.PRE),
            ("program", Phase.POST),
        ]

    @pytest.mark.asyncio
    async def test_post_carries_success_true_on_clean_exit(self):
        root, a, b = make_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="program", phase=Phase.POST)
        async with b.event_emitter("program"):
            pass
        assert events[0].properties["success"] is True

    @pytest.mark.asyncio
    async def test_post_carries_success_false_and_error_on_exception(self):
        root, a, b = make_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="program", phase=Phase.POST)
        with pytest.raises(RuntimeError, match="boom"):
            async with b.event_emitter("program"):
                raise RuntimeError("boom")
        assert events[0].properties["success"] is False
        assert events[0].properties["error_class"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_exception_propagates(self):
        # event_emitter does NOT swallow the exception.
        root, a, b = make_tree()
        with pytest.raises(ValueError):
            async with b.event_emitter("program"):
                raise ValueError("x")

    @pytest.mark.asyncio
    async def test_base_properties_carry_through(self):
        root, a, b = make_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="program")
        async with b.event_emitter("program",
                                    target="my-target", kind="full"):
            pass
        # Base properties present on both PRE and POST.
        for e in events:
            assert e.properties.get("target") == "my-target"
            assert e.properties.get("kind") == "full"

    @pytest.mark.asyncio
    async def test_notifier_progress_fires_progress_phase(self):
        root, a, b = make_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="program")
        async with b.event_emitter("program", target="my") as notifier:
            assert isinstance(notifier, Notifier)
            await notifier.progress(current=1, total=10)
            await notifier.progress(current=5, total=10)
        progress_events = [e for e in events if e.phase == Phase.PROGRESS]
        assert len(progress_events) == 2
        assert progress_events[0].properties == {
            "target": "my", "current": 1, "total": 10}
        assert progress_events[1].properties == {
            "target": "my", "current": 5, "total": 10}

    @pytest.mark.asyncio
    async def test_progress_overrides_base_property(self):
        # Per-tick properties win over base properties when keys
        # collide (standard dict merge semantics).
        root, a, b = make_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            phase=Phase.PROGRESS)
        async with b.event_emitter("program", target="base") as notifier:
            await notifier.progress(target="overridden")
        assert events[0].properties["target"] == "overridden"


class TestAutoStartStopEmits:
    @pytest.mark.asyncio
    async def test_start_fires_pre_then_post(self):
        node = Node("n")
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="start")
        await node.start_tree()
        assert [(e.action, e.phase) for e in events] == [
            ("start", Phase.PRE),
            ("start", Phase.POST),
        ]
        assert events[1].properties["success"] is True

    @pytest.mark.asyncio
    async def test_start_emits_with_node_path(self):
        root, a, b = make_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="start", phase=Phase.POST)
        await root.start_tree()
        # One POST per node (root, a, b), in tree-walk order.
        sources = [e.source for e in events]
        assert sources == ["root", "root/a", "root/a/b"]

    @pytest.mark.asyncio
    async def test_start_post_carries_failure_on_exception(self):
        class FailNode(Node):
            async def start(self):
                raise RuntimeError("nope")

        node = FailNode("n")
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="start", phase=Phase.POST)
        with pytest.raises(RuntimeError):
            await node.start_tree()
        assert events[0].properties["success"] is False
        assert events[0].properties["error_class"] == "RuntimeError"
        assert node.started is False

    @pytest.mark.asyncio
    async def test_idempotent_start_emits_once(self):
        node = Node("n")
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="start")
        await node.start_tree()
        await node.ensure_started()  # already started — should no-op
        await node.start_tree()       # ditto
        # Only one (pre, post) pair from the first call.
        assert len([e for e in events if e.phase == Phase.PRE]) == 1
        assert len([e for e in events if e.phase == Phase.POST]) == 1

    @pytest.mark.asyncio
    async def test_stop_fires_pre_then_post(self):
        node = Node("n")
        await node.start_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="stop")
        await node.stop_tree()
        assert [(e.action, e.phase) for e in events] == [
            ("stop", Phase.PRE),
            ("stop", Phase.POST),
        ]
        assert events[1].properties["success"] is True

    @pytest.mark.asyncio
    async def test_stop_emits_top_down(self):
        root, a, b = make_tree()
        await root.start_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="stop", phase=Phase.POST)
        await root.stop_tree()
        # Parent's POST fires before its children's events.
        sources = [e.source for e in events]
        assert sources == ["root", "root/a", "root/a/b"]


class TestAutoAttachDetachEmits:
    @pytest.mark.asyncio
    async def test_sync_attach_deferred_until_start(self):
        # Sync construction does not emit attach synchronously;
        # the emit is deferred until ensure_started runs.
        seen: list[Event] = []
        get_bus().subscribe(lambda e: seen.append(e), action="attach")
        root = Node("root")
        a = Node("a")
        root.child_add(a)
        await asyncio.sleep(0)  # no async path through a yet
        assert seen == []

        # First ensure_started for `a` drains the pending attach.
        await root.start_tree()
        # Subscribed only to attach — start events filtered out.
        assert len(seen) == 1
        assert seen[0].source == "root/a"
        assert seen[0].phase == Phase.POST
        assert seen[0].properties == {"parent": "root"}

    @pytest.mark.asyncio
    async def test_attach_post_fires_before_start_pre(self):
        # Ordering: for each node, attach POST precedes start PRE.
        seen: list[tuple[str, str, object]] = []
        get_bus().subscribe(
            lambda e: seen.append((e.action, e.phase, e.source)),
            action=("attach", "start"))
        root, a, b = make_tree()
        await root.start_tree()
        # Root has no parent, so no attach. `a` and `b` each get
        # attach POST emitted in the same ensure_started call,
        # immediately before start PRE.
        relevant = [(action, phase, src) for action, phase, src in seen
                    if src in ("root/a", "root/a/b")]
        # Sequence per node: (attach, POST), (start, PRE), (start, POST).
        assert relevant == [
            ("attach", Phase.POST, "root/a"),
            ("start", Phase.PRE, "root/a"),
            ("start", Phase.POST, "root/a"),
            ("attach", Phase.POST, "root/a/b"),
            ("start", Phase.PRE, "root/a/b"),
            ("start", Phase.POST, "root/a/b"),
        ]

    @pytest.mark.asyncio
    async def test_runtime_attach_fires_through_auto_start(self):
        # Parent already started; child_add schedules auto-start
        # via ensure_future, which runs ensure_started, which
        # drains the pending attach.
        root = Node("root")
        await root.start_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="attach")

        child = Node("c")
        root.child_add(child)
        # Let the auto-start task run through ensure_started.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(events) == 1
        assert events[0].source == "root/c"
        assert events[0].phase == Phase.POST
        assert events[0].properties == {"parent": "root"}

    @pytest.mark.asyncio
    async def test_attach_and_detach_fire_without_start(self):
        # Child added in sync setup then removed without ever
        # being started: attach POST drained from child_remove,
        # then detach pre/post fire. start/stop do NOT fire
        # because the child was never live.
        root = Node("root")
        a = Node("a")
        root.child_add(a)
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e))

        await root.child_remove(a)

        actions_phases = [(e.action, e.phase, e.source) for e in events]
        # Exact sequence: drained attach POST, then detach PRE/POST.
        # No start/stop because neither node was started.
        assert actions_phases == [
            ("attach", Phase.POST, "root/a"),
            ("detach", Phase.PRE, "root/a"),
            ("detach", Phase.POST, "root/a"),
        ]

    @pytest.mark.asyncio
    async def test_detach_fires_pre_then_post(self):
        root, a, _b = make_tree()
        await root.start_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="detach")

        await root.child_remove(a)

        # PRE: source = a's path while attached, POST: same path,
        # captured before detach.
        actions = [(e.phase, e.source) for e in events]
        assert (Phase.PRE, "root/a") in actions
        assert (Phase.POST, "root/a") in actions
        # Both PRE and POST carry parent.
        for e in events:
            assert e.properties.get("parent") == "root"

    @pytest.mark.asyncio
    async def test_detach_post_source_is_pre_detach_path(self):
        root, a, _b = make_tree()
        await root.start_tree()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="detach", phase=Phase.POST)

        await root.child_remove(a)
        # `a` is detached now — its current `.path` is just "a"
        # — but the POST event must still carry the path it had
        # while attached.
        assert a.path == "a"
        assert events[0].source == "root/a"

    @pytest.mark.asyncio
    async def test_stop_tree_silent_on_never_started_node(self):
        # stop_tree on a never-started node emits no stop events
        # (symmetric with ensure_started, which only emits start
        # when start() actually runs).
        seen: list[Event] = []
        get_bus().subscribe(lambda e: seen.append(e), action="stop")
        node = Node("n")
        await node.stop_tree()
        assert seen == []

    @pytest.mark.asyncio
    async def test_stop_tree_recurses_even_when_self_not_started(self):
        # If self was never started but a child was (via direct
        # ensure_started), stop_tree still recurses into the child.
        root = Node("root")
        child = Node("child")
        root.child_add(child)
        await child.ensure_started()  # only child is started
        assert child.started is True
        assert root.started is False

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="stop")
        await root.stop_tree()

        # root: no stop event (was never started).
        # child: stop pre/post.
        sources = [e.source for e in events]
        assert sources == ["root/child", "root/child"]


class TestNotifiedDecorator:
    @pytest.mark.asyncio
    async def test_wraps_method_with_event_emitter(self):
        class MyNode(Node):
            @Node.notified("program")
            async def write(self, payload):
                return payload * 2

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="program")
        n = MyNode("n")
        result = await n.write(21)
        assert result == 42
        phases = [e.phase for e in events]
        assert phases == [Phase.PRE, Phase.POST]
        assert events[1].properties["success"] is True

    @pytest.mark.asyncio
    async def test_decorator_propagates_exceptions_with_post(self):
        class MyNode(Node):
            @Node.notified("program")
            async def fail(self):
                raise RuntimeError("nope")

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="program")
        n = MyNode("n")
        with pytest.raises(RuntimeError, match="nope"):
            await n.fail()
        assert events[-1].phase == Phase.POST
        assert events[-1].properties["success"] is False
        assert events[-1].properties["error_class"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_decorated_method_returns_value(self):
        class MyNode(Node):
            @Node.notified("compute")
            async def compute(self, a, b):
                return a + b

        n = MyNode("n")
        assert await n.compute(2, 3) == 5
