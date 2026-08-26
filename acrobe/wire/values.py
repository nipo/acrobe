"""Built-in @wire.value registrations for acrobe domain types.

Imported by `wire/__init__.py` so the registrations land in the
default registry on first wire import.
"""

from ..bitstring import BitString
from . import registry as _registry

_registry.value("a322dc61-7a54-4555-bcf6-81124e665d98")(BitString)
