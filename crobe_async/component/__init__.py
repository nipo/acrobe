import asyncio
import logging
from contextlib import contextmanager

from ..db import NoMatch
from ..log import get_progress


class Component:

    def __init__(self, name: str):
        self._name = name
        self._parent = None
        self._children = []
        self._started = False

    def __str__(self):
        return self._name

    def __repr__(self):
        return f"<{self.__class__.__name__} '{self._name}'>"

    @property
    def name(self) -> str:
        return self._name

    @property
    def parent(self):
        return self._parent

    @property
    def children(self) -> list:
        return list(self._children)

    @property
    def fqdn(self) -> str:
        parts = []
        node = self
        while node is not None:
            parts.append(node._name)
            node = node._parent
        parts.reverse()
        return ".".join(parts)

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(self.fqdn)

    @property
    def started(self) -> bool:
        return self._started

    def child_add(self, child: "Component"):
        assert child._parent is None, f"{child.fqdn} already has a parent"
        child._parent = self
        self._children.append(child)
        self.children_changed()
        if self._started:
            asyncio.ensure_future(child.start_tree())

    async def child_remove(self, child: "Component"):
        assert child._parent is self, f"{child.fqdn} is not a child of {self.fqdn}"
        await child.stop_tree()
        self._children.remove(child)
        child._parent = None
        self.children_changed()

    def children_changed(self):
        pass

    def children_find(self, predicate, include_self=False) -> list:
        result = []
        if include_self and predicate(self):
            result.append(self)
        for child in self._children:
            result.extend(child.children_find(predicate, include_self=True))
        return result

    def children_of_class(self, klass, include_self=False) -> list:
        return self.children_find(lambda c: isinstance(c, klass), include_self=include_self)

    def parent_of_class(self, klass):
        node = self._parent
        while node is not None:
            if isinstance(node, klass):
                return node
            node = node._parent
        raise LookupError(f"No ancestor of class {klass.__name__} found from {self.fqdn}")

    @contextmanager
    def progress(self, label, total, unit=""):
        """Context manager for progress tracking.

        Delegates to the global ProgressDelegate (set via log.set_progress).

        Usage:
            with self.progress("Erasing", total=16, unit="sectors") as p:
                for sector in sectors:
                    await self._erase_sector(sector)
                    p.advance()
        """
        handle = get_progress().create(self.fqdn, label, total, unit)
        try:
            yield handle
        finally:
            handle.close()

    def child_lookup(self, name):
        """Find existing child by name.

        Supports ".." for parent, "*" for single child, int index,
        and case-insensitive substring match (unique match only).
        Returns None if not found.
        """
        if name == "..":
            return self._parent
        if name == "*":
            if len(self._children) == 1:
                return self._children[0]
            return None
        try:
            return self._children[int(name)]
        except (ValueError, IndexError):
            pass
        matches = [c for c in self._children if name.lower() in c._name.lower()]
        if len(matches) == 1:
            return matches[0]
        return None

    async def child_spawn(self, name):
        """Create a child by name. Override in subclasses."""
        raise NoMatch("child", name)

    async def child_summon(self, *parts):
        """Resolve a path through the component tree, spawning as needed.

        Spawned children that are Components are added to the tree.
        Non-Component children (e.g. Batcher-only objects) are returned
        without tree insertion.
        """
        if not parts:
            return self
        name, *rest = parts
        child = self.child_lookup(name)
        if child is None:
            child = await self.child_spawn(name)
            if isinstance(child, Component) and child._parent is None:
                self.child_add(child)
        if not rest:
            return child
        return await child.child_summon(*rest)

    async def start(self):
        pass

    async def stop(self):
        pass

    async def start_tree(self):
        await self.start()
        self._started = True
        for child in self._children:
            await child.start_tree()

    async def stop_tree(self):
        await self.stop()
        self._started = False
        for child in self._children:
            await child.stop_tree()
