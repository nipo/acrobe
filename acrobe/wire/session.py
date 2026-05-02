"""Session — per-connection tag table for the wire protocol.

Two phases:

1. **Server side.** `build_catalog(node_class)` allocates a session-local
   tag for the node and for each Transportable in its `uses` set, plus
   the always-on wire-level error types. Returns a `Catalog` frame to
   send to the client. The server-side Session is now ready to encode
   and decode.

2. **Client side.** `apply_catalog(catalog)` reads the catalog and
   installs the same mapping. UUIDs the client doesn't know are
   silently ignored — the client can still talk about everything in
   the intersection. The node UUID itself MUST be known; otherwise
   the connection isn't viable and the caller raises.

After phase 1 or 2, both sides hold matching `class_to_tag` /
`tag_to_class` dicts and use them for value encoding.

Tag space starts at `BASE_SESSION_TAG` (configurable, default 100)
and increments. Tags are session-local; nothing IANA-registered
matters here.
"""

import uuid as uuid_lib
from typing import Any

import cbor2

from .frame import Catalog
from .registry import Registry, RegistryEntry


BASE_SESSION_TAG = 100


class SessionError(Exception):
    """Raised when a Session can't encode / decode a value or apply a
    catalog. Distinct from CodecError (per-class layout) and FrameError
    (frame-level parsing).
    """


class Session:
    """Per-connection tag table + value encoder/decoder.

    Always-on wire-level types (currently just `InternalError`) are
    auto-registered into every session by `build_catalog` and
    `apply_catalog`, regardless of the node's declared `uses`. Their
    UUIDs are listed in `Session.ALWAYS_ON`.
    """

    ALWAYS_ON: tuple[uuid_lib.UUID, ...] = ()  # populated lazily

    def __init__(self, registry: Registry):
        self.registry = registry
        self.class_to_tag: dict[type, int] = {}
        self.tag_to_class: dict[int, type] = {}
        self._next_tag = BASE_SESSION_TAG

    # ----- Catalog issuance / application -----

    def build_catalog(self, node_class: type) -> Catalog:
        """Server-side: assign tags for `node_class`'s interface and
        return a Catalog frame for the client.

        The node class itself gets a tag too, so future protocol
        extensions can refer to it directly; for v1 the value isn't
        used over the wire.
        """
        node_entry = self.registry.lookup_by_class(node_class)
        if node_entry.kind != "node":
            raise SessionError(
                f"build_catalog: {node_class.__name__} is not a "
                f"@wire.node ({node_entry.kind})")

        node_tag = self._allocate_tag(node_class)
        types: dict[uuid_lib.UUID, int] = {}

        for type_uuid in self._effective_uses(node_entry):
            type_entry = self.registry.lookup_by_uuid(type_uuid)
            tag = self._allocate_tag(type_entry.cls)
            types[type_uuid] = tag

        return Catalog(node_uuid=node_entry.type_uuid,
                       node_tag=node_tag,
                       types=types)

    def apply_catalog(self, catalog: Catalog) -> None:
        """Client-side: install tag mapping from a received catalog.

        Unknown type UUIDs are skipped silently — the client just
        won't be able to use those types. The node UUID must be known.
        """
        try:
            node_entry = self.registry.lookup_by_uuid(catalog.node_uuid)
        except KeyError as exc:
            raise SessionError(
                f"apply_catalog: node UUID {catalog.node_uuid} not "
                f"in registry; client and server are incompatible"
            ) from exc

        self._install(node_entry.cls, catalog.node_tag)

        for type_uuid, session_tag in catalog.types.items():
            try:
                type_entry = self.registry.lookup_by_uuid(type_uuid)
            except KeyError:
                continue
            self._install(type_entry.cls, session_tag)

    def _effective_uses(self, node_entry: RegistryEntry) -> list[uuid_lib.UUID]:
        """The node's declared `uses` plus the always-on wire-level types
        present in this Session's registry. Always-on types missing from
        a (typically test-isolated) registry are silently skipped."""
        seen = set(node_entry.uses)
        result = list(node_entry.uses)
        for uid in self.ALWAYS_ON:
            if uid in seen:
                continue
            try:
                self.registry.lookup_by_uuid(uid)
            except KeyError:
                continue
            result.append(uid)
            seen.add(uid)
        return result

    def _allocate_tag(self, cls: type) -> int:
        if cls in self.class_to_tag:
            return self.class_to_tag[cls]
        tag = self._next_tag
        self._next_tag += 1
        self._install(cls, tag)
        return tag

    def _install(self, cls: type, tag: int) -> None:
        existing_tag = self.class_to_tag.get(cls)
        if existing_tag is not None and existing_tag != tag:
            raise SessionError(
                f"{cls.__name__} already mapped to tag {existing_tag}, "
                f"refused remap to {tag}")
        existing_cls = self.tag_to_class.get(tag)
        if existing_cls is not None and existing_cls is not cls:
            raise SessionError(
                f"tag {tag} already mapped to {existing_cls.__name__}, "
                f"refused remap to {cls.__name__}")
        self.class_to_tag[cls] = tag
        self.tag_to_class[tag] = cls

    # ----- Per-value encode/decode -----

    def encode_value(self, value: Any) -> Any:
        """Convert a Python value into a cbor2-encodable form,
        wrapping registered Transportables in CBOR tags by their
        session-local tag.
        """
        if value is None or isinstance(value, (int, str, bytes, bool, float)):
            return value
        if isinstance(value, (list, tuple)):
            return [self.encode_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.encode_value(v) for k, v in value.items()}

        cls = type(value)
        entry = self.registry.try_lookup_by_class(cls)
        if entry is None:
            raise SessionError(
                f"cannot encode {cls.__name__}: not a registered "
                f"Transportable")
        tag = self.class_to_tag.get(cls)
        if tag is None:
            raise SessionError(
                f"cannot encode {cls.__name__}: not in session catalog")
        return cbor2.CBORTag(tag, entry.codec.encode(value))

    def decode_value(self, value: Any) -> Any:
        if isinstance(value, cbor2.CBORTag):
            cls = self.tag_to_class.get(value.tag)
            if cls is None:
                raise SessionError(
                    f"cannot decode CBOR tag {value.tag}: not in "
                    f"session catalog")
            entry = self.registry.lookup_by_class(cls)
            return entry.codec.decode(value.value)
        if isinstance(value, list):
            return [self.decode_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.decode_value(v) for k, v in value.items()}
        return value
