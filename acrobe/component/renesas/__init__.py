"""Renesas-specific hardware components.

Importing this package triggers the registration of Renesas-designed
CoreSight components against :class:`MemoryMappedComponent`'s PartId
registry."""

from . import coresight  # noqa: F401
