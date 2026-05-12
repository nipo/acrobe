"""Target — operational view on hardware.

A `Target` is a Node that gathers operational views on one or more
components. Views are children of the Target: `Loadable` for
programming, `Debuggable` for run-control, `Puppet` for trampoline
exec, `DebugAuth` for keyed-debug, etc.

Targets are discovered from the component tree via `@Target.register`
and parented flat under the root by `TargetDiscovery`. References to
component nodes are plain attributes; the component tree owns those
nodes, the Target only borrows them.

Discovery state — `claimed_components()` — declares which component
nodes a Target consumed at construction time. `TargetDiscovery` reads
this to avoid re-spawning a Target for components already in use.
"""

from dataclasses import dataclass
from typing import Callable

from ..node import Node


@dataclass
class Explorer:
    """One `@Target.register` entry.

    `func`             — callable invoked with the matched component.
                          Returns a Target or raises NoMatch /
                          NotImplementedError to skip.
    `component_types`  — tuple of classes the entry applies to;
                          matched via `isinstance`.
    `precedence`       — lower runs first.
    """

    func: Callable
    component_types: tuple
    precedence: int

    def __lt__(self, other):
        return self.precedence < other.precedence


class Target(Node):
    """Base Target. Plain container Node — no programming methods.

    Subclasses wire their own view children (Loadable, Debuggable,
    etc.) in `__init__`. Subclasses that consume more than the
    discovery component must override `claimed_components` to list
    every component node they reference, so `TargetDiscovery` can
    avoid double-claiming.
    """

    explorers: list = []

    @classmethod
    def register(cls, *component_types, precedence=1000):
        """Decorator: register a discovery entry.

        Decorated callable receives the matched component and
        returns a Target instance. Raise NoMatch / NotImplementedError
        to decline the match.
        """
        def decorator(func):
            cls.explorers.append(
                Explorer(func, component_types, precedence))
            cls.explorers.sort()
            return func
        return decorator

    def __init__(self, name):
        super().__init__(name)
        self.claims = set()

    def claim(self, *components):
        """Declare component nodes consumed by this Target.

        The discovery component (the one matched by `@Target.register`)
        is added automatically by `TargetDiscovery`; subclasses call
        `claim` for any *additional* components they reference.
        """
        self.claims.update(components)

    def claimed_components(self):
        """Return the set of component nodes this Target consumed."""
        return frozenset(self.claims)
