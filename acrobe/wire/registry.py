"""Transportable type registry and the `@wire.op`/`@wire.error`/`@wire.node`
decorators.

Each decoration produces a `RegistryEntry` keyed by the class's UUID.
The decorators run validation at import time:

* UUID well-formed and unique.
* For ops/errors: codec is buildable (dataclass + supported types, or
  custom codec methods present).
* For nodes: every entry in `uses=[...]` has already been registered
  as op or error; the decorated class is a `Node` subclass and a
  `Batcher` subclass.

A single global `Registry` instance is exposed via the module-level
decorators. Tests that need isolation construct their own.
"""

import uuid as uuid_lib
from typing import Iterable

from .codec import _Codec, build_codec


class RegistryError(Exception):
    """Raised at decoration time when a class can't be registered."""


class RegistryEntry:
    """Per-registered-class record.

    Holds the canonical UUID, the kind (`op`/`error`/`node`), the
    codec, and (for nodes) the `uses` set of referenced UUIDs.
    """

    __slots__ = ("cls", "type_uuid", "kind", "codec", "uses")

    def __init__(self, cls: type, type_uuid: uuid_lib.UUID, kind: str,
                 codec: _Codec | None, uses: tuple = ()):
        self.cls = cls
        self.type_uuid = type_uuid
        self.kind = kind
        self.codec = codec
        self.uses = uses

    def __repr__(self):
        return (f"<RegistryEntry {self.kind} {self.cls.__name__} "
                f"{self.type_uuid}>")


class Registry:
    """In-memory catalog of every Transportable class declared so far.

    Indexed both by UUID and by class. Decorators register here; the
    codec follows references through it; debug.dump_idl walks it.
    """

    def __init__(self):
        self.__by_uuid: dict[uuid_lib.UUID, RegistryEntry] = {}
        self.__by_class: dict[type, RegistryEntry] = {}

    def register(self, cls: type, kind: str, type_uuid_str: str,
                 uses: Iterable[type] = ()) -> RegistryEntry:
        try:
            type_uuid = uuid_lib.UUID(type_uuid_str)
        except (ValueError, AttributeError, TypeError) as exc:
            raise RegistryError(
                f"{cls.__name__}: invalid UUID {type_uuid_str!r}") from exc

        if type_uuid in self.__by_uuid:
            existing = self.__by_uuid[type_uuid]
            raise RegistryError(
                f"UUID {type_uuid} already registered to "
                f"{existing.cls.__name__}; cannot reuse for "
                f"{cls.__name__}")

        if cls in self.__by_class:
            raise RegistryError(
                f"{cls.__name__} already registered (uuid="
                f"{self.__by_class[cls].type_uuid})")

        if kind == "node":
            uses_tuple = tuple(self.__validate_node_use(cls, u) for u in uses)
            codec = None
        elif kind in ("op", "error", "value"):
            if list(uses):
                raise RegistryError(
                    f"{cls.__name__}: 'uses' is only valid on @wire.node")
            codec = build_codec(cls, self)
            uses_tuple = ()
        else:
            raise RegistryError(f"unknown kind {kind!r}")

        entry = RegistryEntry(cls, type_uuid, kind, codec, uses_tuple)
        self.__by_uuid[type_uuid] = entry
        self.__by_class[cls] = entry
        # Annotate the class with the registry mapping when the
        # type permits class-attribute assignment. Cython cdef
        # classes (and other immutable types) reject this — the
        # canonical mapping is __by_class anyway, so silently
        # tolerate the rejection.
        try:
            cls.__wire_uuid__ = type_uuid
            cls.__wire_kind__ = kind
        except TypeError:
            pass
        return entry

    def __validate_node_use(self, node_cls: type,
                            used: type) -> uuid_lib.UUID:
        entry = self.__by_class.get(used)
        if entry is None:
            raise RegistryError(
                f"{node_cls.__name__}: uses={used.__name__!r} which is not "
                f"a registered Transportable. Apply @wire.op or @wire.error "
                f"first.")
        if entry.kind not in ("op", "error"):
            raise RegistryError(
                f"{node_cls.__name__}: uses={used.__name__!r} is a "
                f"{entry.kind}, not an op or error.")
        return entry.type_uuid

    def lookup_by_uuid(self, type_uuid: uuid_lib.UUID) -> RegistryEntry:
        return self.__by_uuid[type_uuid]

    def lookup_by_class(self, cls: type) -> RegistryEntry:
        return self.__by_class[cls]

    def try_lookup_by_class(self, cls: type) -> RegistryEntry | None:
        return self.__by_class.get(cls)

    def all_entries(self) -> Iterable[RegistryEntry]:
        return self.__by_uuid.values()

    def nodes(self) -> Iterable[RegistryEntry]:
        return (e for e in self.__by_uuid.values() if e.kind == "node")


_default_registry = Registry()


def default_registry() -> Registry:
    return _default_registry


def op(type_uuid: str):
    """Register a class as a Transportable operation."""
    def decorator(cls):
        _default_registry.register(cls, "op", type_uuid)
        return cls
    return decorator


def error(type_uuid: str):
    """Register a class as a Transportable error.

    The class need not subclass Exception, but doing so is the obvious
    choice — server-side will raise the instance, client-side will
    re-raise it.
    """
    def decorator(cls):
        _default_registry.register(cls, "error", type_uuid)
        return cls
    return decorator


def value(type_uuid: str):
    """Register a class as a Transportable value type.

    Value types appear as field types inside @wire.op / @wire.error
    payloads, but are not themselves posted as ops or raised as
    errors. They do NOT belong in any node's `uses=[...]` — that list
    is for ops and errors only. Value types still need a codec
    (dataclass introspection or __cbor_encode__/__cbor_decode__).
    """
    def decorator(cls):
        _default_registry.register(cls, "value", type_uuid)
        return cls
    return decorator


def node(type_uuid: str, *, uses=()):
    """Register a Node+Batcher class as a Transportable node.

    `uses` lists the op and error classes this node may exchange over
    the wire. Mixed list — no semantic split between commands and
    errors at the node level.
    """
    def decorator(cls):
        from ..node import Node
        from ..engine import Batcher
        if not issubclass(cls, Node):
            raise RegistryError(
                f"{cls.__name__}: @wire.node target must subclass Node")
        if not issubclass(cls, Batcher):
            raise RegistryError(
                f"{cls.__name__}: @wire.node target must subclass Batcher "
                f"(only Batchers are transportable in v1)")
        _default_registry.register(cls, "node", type_uuid, uses=uses)
        return cls
    return decorator
