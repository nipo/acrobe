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
from .frame import (
    Catalog,
    FrameError,
    ProtocolError,
    Request,
    Response,
    decode_frame,
    encode_catalog,
    encode_protocol_error,
    encode_request,
    encode_response,
)
from .principal import Principal, Scope
from .registry import (
    Registry,
    RegistryEntry,
    RegistryError,
    default_registry,
    error,
    node,
    op,
    value,
)
from .session import Session, SessionError
# errors imports session, so keep it last to avoid circulars.
from .errors import InternalError  # noqa: E402  (registers in default registry)
from .dispatch import handle_request  # noqa: E402
from . import values  # noqa: E402,F401  (registers BitString)
from .enumerator import (  # noqa: E402
    RemoteProxyNode,
    RemoteServerRoot,
    WireEnumerator,
    WireNamespace,
)

__all__ = [
    "AuthBackend",
    "Catalog",
    "CodecError",
    "FrameError",
    "InternalError",
    "OpenAuthBackend",
    "Principal",
    "ProtocolError",
    "Registry",
    "RegistryEntry",
    "RegistryError",
    "Request",
    "Response",
    "Scope",
    "Session",
    "SessionError",
    "audit_log",
    "decode_frame",
    "default_registry",
    "dump_idl",
    "encode_catalog",
    "encode_protocol_error",
    "encode_request",
    "encode_response",
    "error",
    "from_bytes",
    "handle_request",
    "node",
    "op",
    "RemoteProxyNode",
    "RemoteServerRoot",
    "WireEnumerator",
    "WireNamespace",
    "to_bytes",
    "value",
]
