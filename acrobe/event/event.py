"""Event-bus value types: Event dataclass, Phase enum, Action
canonical names.

Events are immutable observations published on the global bus.
Subscribers read them; producers never reuse an Event instance for
two emits.

Path comparison helpers (`source_is`, `source_under`) on Event let
subscribers write predicate filters without importing the
underlying `Path` utilities for the common cases.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

import time

from ..node import Path


class Phase(StrEnum):
    """When along an action's lifetime an event fires.

    `PRE`  — emitter is about to start the action.
    `POST` — emitter has just finished it (success/failure carried
             in properties).
    `PROGRESS` — emitted from within an in-flight action.

    Observation-shaped events (filesystem changes, hotplug
    notifications dispatched by the OS) carry `phase=None` —
    there is no pre/post split because the work happened outside
    our control.
    """

    PRE = "pre"
    POST = "post"
    PROGRESS = "progress"


class Action:
    """Canonical action names. Third-party publishers may emit
    other strings; these are the names the core uses and the
    names cross-cutting subscribers should know.

    Two flavours:

    - **Intent** — fired around something acrobe code is about
      to do or just did. Pre/post is meaningful.
    - **Observation** — fired in response to something the OS
      already did. `phase` is None.
    """

    # Intent (pre/post meaningful)
    ATTACH = "attach"
    DETACH = "detach"
    START = "start"
    STOP = "stop"
    PROGRAM = "program"
    RESET = "reset"
    MUTATE = "mutate"

    # Observation (phase=None)
    CREATED = "created"
    CHANGED = "changed"
    DELETED = "deleted"
    MOVED = "moved"
    TOUCHED = "touched"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class Event:
    """One observation published on the bus.

    `source`   — canonical path of the publisher at emit time.
                 Slash form, matching `Node.path` for hardware
                 or absolute POSIX path for filesystem.
    `action`   — what happened (see `Action` for canonical names).
    `phase`    — when along the action's lifetime (`Phase`), or
                 None for observation-shaped events.
    `properties` — free-form mapping of additional context.
                   Per-action contract documented alongside the
                   canonical actions.
    `timestamp` — monotonic time at emit. Useful for ordering
                  in logs; not for wall-clock display.
    """

    source: str
    action: str
    phase: str | None = None
    properties: Mapping[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)

    def source_is(self, path: str) -> bool:
        """True if this event's source equals `path` exactly."""
        return self.source == path

    def source_under(self, path: str) -> bool:
        """True if this event's source equals `path` or is a
        descendant of it. Empty `path` matches every event."""
        return Path.is_descendant_or_self(self.source, path)


class Notifier:
    """Handle yielded by `Node.event_emitter()` so the body of the
    action can emit `Phase.PROGRESS` events sharing the action's
    base properties.

    Holds a Node reference (only for its `emit` method), the
    action name, and the base properties the emitter was opened
    with. `progress(**props)` merges those base properties with
    the per-tick ones and emits.
    """

    def __init__(self, node, action: str, base_properties: Mapping[str, Any]):
        self.__node = node
        self.__action = action
        self.__base_properties = base_properties

    async def progress(self, **properties) -> None:
        """Emit a PROGRESS-phase event under the active action."""
        merged = {**self.__base_properties, **properties}
        await self.__node.emit(self.__action,
                               phase=Phase.PROGRESS, **merged)
