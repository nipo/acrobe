"""Wire — client/server transport layer for acrobe Batchers.

Public surface:

* `wire.op`, `wire.error`, `wire.node` — class decorators.
* `wire.Principal`, `wire.Scope` — auth value objects.
* `wire.AuthBackend`, `wire.OpenAuthBackend` — auth interface and the
  no-auth default.
* `wire.audit_log` — no-op audit hook.
* `wire.dump_idl` — debug introspection.
* `wire.to_bytes`, `wire.from_bytes` — codec roundtrip helpers, mostly
  for tests; production code uses Frame-level encoding (later phase).

See PLAN_wire.md for the full design.
"""

from .auth import AuthBackend, OpenAuthBackend, audit_log
from .codec import CodecError, from_bytes, to_bytes
from .debug import dump_idl
from .principal import Principal, Scope
from .registry import (
    Registry,
    RegistryEntry,
    RegistryError,
    default_registry,
    error,
    node,
    op,
)

__all__ = [
    "AuthBackend",
    "CodecError",
    "OpenAuthBackend",
    "Principal",
    "Registry",
    "RegistryEntry",
    "RegistryError",
    "Scope",
    "audit_log",
    "default_registry",
    "dump_idl",
    "error",
    "from_bytes",
    "node",
    "op",
    "to_bytes",
]
