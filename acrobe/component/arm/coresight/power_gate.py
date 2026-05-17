"""Power-gated / unreachable component placeholder.

When a ROM Table entry's component can't be classified (read fault,
all-zero IDs, secure lockup, or simply nothing at the address), the
walker installs a :class:`PowerGate` at that address. Discovery is
passive — the walker never enables a power domain, never wakes a
component, never touches a security lock. The Target framework,
when it lands, may walk gates and call :meth:`PowerGate.retry`
after explicitly powering up the relevant domain via a chip-specific
driver.

The gate is the parent of any component that ``retry()`` discovers.
We don't reclassify the gate Node into a different type (no Node
type promotion); instead we keep the gate as a passive parent and
attach the real component as its child.
"""

from __future__ import annotations

from enum import IntEnum

from ....node import Node


class FailureKind(IntEnum):
    """Why a child wasn't classifiable at walk time."""
    FAULT         = 0   # ID read raised DpAccessFailure
    EMPTY         = 1   # CIDR preamble didn't match (all zero, garbage)
    SECURE_LOCKED = 2   # Component reads OK but is access-protected
    UNKNOWN       = 3   # Reserved for future failure modes


class PowerGate(Node):
    """Passive placeholder for a child that wasn't discoverable.

    A gate is created by :class:`RomTable` when it can't classify
    the entry at ``address``. It remembers the address and the
    failure kind so callers (typically the Target framework) can
    decide whether and how to power up the underlying component
    and retry."""

    def __init__(self, bus, address: int, kind: FailureKind,
                 name: str | None = None):
        if name is None:
            name = f"gate@{address:08x}({kind.name})"
        super().__init__(name)
        self._bus = bus
        self.address = address
        self.failure_kind = kind

    async def retry(self):
        """Re-attempt discovery at ``self.address``. On success,
        attach the discovered component as a child of this gate.
        Returns the new child or ``None`` if still unreachable.

        Imports lazily to avoid a circular module-load with model.py."""
        from .model import ComponentIds, MemoryMappedComponent, _pick_class
        from ..dp import DpAccessFailure

        try:
            ids = await ComponentIds.read(self._bus, self.address)
        except DpAccessFailure as exc:
            self.logger.warning(
                "PowerGate retry at 0x%x: ID read still failing: %s",
                self.address, exc)
            return None

        if ids.cidr_class is None:
            self.logger.info(
                "PowerGate retry at 0x%x: still empty (no CIDR preamble)",
                self.address)
            return None

        chosen = _pick_class(ids)
        try:
            child = chosen(self._bus, self.address, ids)
        except Exception as exc:
            self.logger.warning(
                "PowerGate retry at 0x%x: %s.__init__ raised %s",
                self.address, chosen.__name__, exc, exc_info=True)
            return None

        self.child_add(child)
        self.logger.info(
            "PowerGate retry at 0x%x: discovered %r",
            self.address, child)
        return child

    def __repr__(self):
        return (f"<PowerGate {self.name} address=0x{self.address:08x} "
                f"kind={self.failure_kind.name}>")
