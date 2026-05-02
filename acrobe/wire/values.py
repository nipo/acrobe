"""Built-in @wire.value registrations for acrobe domain types.

Imported by `wire/__init__.py` so the registrations land in the
default registry on first wire import.
"""

from ..bitstring import BitString
from . import registry as _registry


BITSTRING_UUID = "00000000-0000-4000-8000-000000000010"


_registry.value(BITSTRING_UUID)(BitString)
