"""Adapter enumerator for AJI client paths.

Plugs into :func:`acrobe.adapter.model.make_hw_root`. The single
matched name is ``"aji"``: anything else gets passed through to the
next enumerator. The broker handles the rest of the path.
"""

from ...db import NoMatch
from .broker import AjiBroker


class AjiEnumerator:
    """Returns an :class:`AjiBroker` for the literal name ``"aji"``."""

    async def spawn(self, name: str) -> AjiBroker:
        if name != "aji":
            raise NoMatch("adapter", name)
        return AjiBroker()

    async def scan(self) -> list:
        # No network discovery: AJI servers aren't broadcast-discoverable.
        return []
