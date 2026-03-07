import asyncio
import logging
from contextlib import contextmanager

from ..db import NoMatch
from ..log import get_progress


class Component:
    """Base class for the hardware component tree.

    Children lifecycle:
    - child_add(child) attaches a child. If the parent is already
      started, the child's start_tree() is scheduled automatically
      via ensure_future. Safe to call from __init__ (parent won't
      be started yet).
    - child_remove(child) is async: it stops the child's entire
      subtree (stop_tree), then detaches it.
    - start_tree() walks top-down: calls start() on self, marks
      started, then recurses into existing children.
    - stop_tree() walks top-down: calls stop() on self, clears
      started, then recurses into children.

    Path resolution:
    - child_lookup(name) finds an existing child by name (substring
      match), index, "*" (single child), or ".." (parent).
    - child_spawn(name) creates a new child on demand. Override in
      subclasses. Raises NoMatch by default.
    - child_summon(*parts) walks a path, looking up or spawning at
      each step. Spawned Components are added to the tree (which
      triggers auto-start if parent is started).
    """

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

    def _child_attach(self, child: "Component"):
        """Attach child without auto-start. Used by child_summon."""
        assert child._parent is None, f"{child.fqdn} already has a parent"
        child._parent = self
        self._children.append(child)
        self.children_changed()

    def child_add(self, child: "Component"):
        self._child_attach(child)
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

    def option_set(self, opt):
        """Apply an option to this component. Override in subclasses."""
        raise ValueError(f"Unknown option: {opt!r}")

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

    async def _child_spawn_mro(self, name):
        """Walk the MRO trying each class's child_spawn from __dict__.

        Each class in the hierarchy can define its own child_spawn
        with its own Db. NoMatch causes fallback to the next class.
        """
        for cls in type(self).__mro__:
            try:
                method = cls.__dict__["child_spawn"]
            except KeyError:
                continue
            try:
                return await method(self, name)
            except NoMatch:
                continue
        raise NoMatch("child", name)

    @staticmethod
    def _parse_options(name):
        """Parse "name(opt1,opt2)" into (name, [opt1, opt2]).

        Returns (name, []) if no options are present.
        """
        if name.endswith(")"):
            paren = name.index("(")
            opts_str = name[paren + 1:-1]
            bare_name = name[:paren]
            opts = [o.strip() for o in opts_str.split(",") if o.strip()]
            return bare_name, opts
        return name, []

    async def child_summon(self, *parts):
        """Resolve a path through the component tree, spawning as needed.

        Spawned children that are Components are added to the tree.
        Each component along the path is started (if not already) before
        navigating deeper, so that start() can populate children
        (e.g. Chain.start() discovers TAPs).

        Supports "name(opt1,opt2)" syntax: options are applied via
        option_set() before the component is started.
        """
        if not parts:
            return self
        raw_name, *rest = parts
        bare_name, opts = self._parse_options(raw_name)
        child = self.child_lookup(bare_name)
        if child is None:
            child = await self._child_spawn_mro(bare_name)
            if isinstance(child, Component) and child._parent is None:
                self._child_attach(child)
        if isinstance(child, Component):
            for opt in opts:
                child.option_set(opt)
            if not child._started:
                await child.start()
                child._started = True
        if not rest:
            return child
        return await child.child_summon(*rest)

    async def start(self):
        pass

    async def stop(self):
        pass

    async def start_tree(self):
        if not self._started:
            await self.start()
            self._started = True
        for child in self._children:
            await child.start_tree()

    async def stop_tree(self):
        await self.stop()
        self._started = False
        for child in self._children:
            await child.stop_tree()


from . import xilinx  # noqa: F401, E402
from . import gowin  # noqa: F401, E402
from . import lattice  # noqa: F401, E402
from . import spi_flash  # noqa: F401, E402
