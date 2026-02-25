import asyncio
import pytest
from crobe_async.component import Component


class TestBasicTree:
    def test_name(self):
        c = Component("root")
        assert c.name == "root"

    def test_fqdn_root(self):
        c = Component("root")
        assert c.fqdn == "root"

    def test_fqdn_nested(self):
        root = Component("root")
        child = Component("child")
        root.child_add(child)
        assert child.fqdn == "root.child"

    def test_child_add(self):
        root = Component("root")
        child = Component("child")
        root.child_add(child)
        assert child in root.children
        assert child.parent is root

    def test_child_remove(self):
        root = Component("root")
        child = Component("child")
        root.child_add(child)
        root.child_remove(child)
        assert child not in root.children
        assert child.parent is None

    def test_add_already_parented_raises(self):
        root = Component("root")
        other = Component("other")
        child = Component("child")
        root.child_add(child)
        with pytest.raises(AssertionError):
            other.child_add(child)

    def test_remove_wrong_parent_raises(self):
        root = Component("root")
        other = Component("other")
        child = Component("child")
        root.child_add(child)
        with pytest.raises(AssertionError):
            other.child_remove(child)

    def test_children_returns_copy(self):
        root = Component("root")
        child = Component("child")
        root.child_add(child)
        children = root.children
        children.clear()
        assert len(root.children) == 1


class TestTreeSearch:
    def setup_method(self):
        self.root = Component("root")
        self.a = Component("a")
        self.b = Component("b")
        self.a1 = Component("a1")
        self.root.child_add(self.a)
        self.root.child_add(self.b)
        self.a.child_add(self.a1)

    def test_children_find(self):
        found = self.root.children_find(lambda c: c.name.startswith("a"))
        assert self.a in found
        assert self.a1 in found
        assert self.b not in found

    def test_children_find_include_self(self):
        found = self.root.children_find(lambda c: True, include_self=True)
        assert self.root in found

    def test_children_of_class(self):
        class SpecialComponent(Component):
            pass

        root = Component("root")
        special = SpecialComponent("special")
        normal = Component("normal")
        root.child_add(special)
        root.child_add(normal)

        found = root.children_of_class(SpecialComponent)
        assert special in found
        assert normal not in found

    def test_parent_of_class(self):
        class Adapter(Component):
            pass

        adapter = Adapter("adapter")
        child = Component("child")
        grandchild = Component("grandchild")
        adapter.child_add(child)
        child.child_add(grandchild)

        assert grandchild.parent_of_class(Adapter) is adapter

    def test_parent_of_class_not_found(self):
        root = Component("root")
        child = Component("child")
        root.child_add(child)

        class Missing(Component):
            pass

        with pytest.raises(LookupError):
            child.parent_of_class(Missing)


class TestChildrenChanged:
    def test_called_on_add(self):
        calls = []

        class Tracking(Component):
            def children_changed(self):
                calls.append("changed")

        root = Tracking("root")
        root.child_add(Component("child"))
        assert len(calls) == 1

    def test_called_on_remove(self):
        calls = []

        class Tracking(Component):
            def children_changed(self):
                calls.append("changed")

        root = Tracking("root")
        child = Component("child")
        root.child_add(child)
        calls.clear()
        root.child_remove(child)
        assert len(calls) == 1


class TestAsyncLifecycle:
    @pytest.mark.asyncio
    async def test_start_tree(self):
        order = []

        class Tracked(Component):
            async def start(self):
                order.append(self.name)

        root = Tracked("root")
        child = Tracked("child")
        grandchild = Tracked("grandchild")
        root.child_add(child)
        child.child_add(grandchild)

        await root.start_tree()
        # Top-down: root first, then child, then grandchild
        assert order == ["root", "child", "grandchild"]
        assert root.started
        assert child.started
        assert grandchild.started

    @pytest.mark.asyncio
    async def test_stop_tree(self):
        order = []

        class Tracked(Component):
            async def stop(self):
                order.append(self.name)

        root = Tracked("root")
        child = Tracked("child")
        root.child_add(child)

        await root.start_tree()
        await root.stop_tree()
        # Top-down: root first, then child
        assert order == ["root", "child"]
        assert not root.started
        assert not child.started

    @pytest.mark.asyncio
    async def test_start_adds_children(self):
        """start() may add children during discovery; they should be started too."""

        class Discoverer(Component):
            async def start(self):
                self.child_add(Component("discovered"))

        root = Discoverer("root")
        await root.start_tree()
        assert len(root.children) == 1
        assert root.children[0].name == "discovered"
        assert root.children[0].started

    @pytest.mark.asyncio
    async def test_partial_teardown(self):
        root = Component("root")
        a = Component("a")
        b = Component("b")
        root.child_add(a)
        root.child_add(b)

        await root.start_tree()
        assert a.started and b.started

        await a.stop_tree()
        assert not a.started
        assert b.started  # b unaffected
