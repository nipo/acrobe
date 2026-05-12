"""Wire-level error types — registered Transportables that travel
on every session regardless of a node's declared `uses`.

`InternalError` is the catchall: anything raised server-side that
isn't a registered error gets wrapped in one of these (carrying
`repr`) before transit. Clients re-raise it just like any other
typed wire error.
"""

from dataclasses import dataclass

from . import registry as _registry
from .session import Session

INTERNAL_ERROR_UUID = "cb66c7db-8ef1-4322-b65b-7867229c0087"

@_registry.error(INTERNAL_ERROR_UUID)
@dataclass
class InternalError(Exception):
    """Wraps a server-side exception that wasn't a registered wire error."""

    representation: str

    def __str__(self):
        return f"InternalError({self.representation})"


# Make every Session catalog include InternalError automatically.
import uuid as _uuid_lib  # noqa: E402

Session.ALWAYS_ON = (_uuid_lib.UUID(INTERNAL_ERROR_UUID),)
