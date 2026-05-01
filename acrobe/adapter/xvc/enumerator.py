"""Adapter enumerator for XVC client paths.

Plugs into :func:`acrobe.adapter.model.make_hw_root`. The single
matched name is ``"xvc"``: anything else gets passed through to the
next enumerator. The broker handles the rest of the path.
"""

from ...db import NoMatch
from .broker import XvcBroker


class XvcEnumerator:
    """Returns an :class:`XvcBroker` for the literal name ``"xvc"``."""

    async def spawn(self, name: str) -> XvcBroker:
        if name != "xvc":
            raise NoMatch("adapter", name)
        return XvcBroker()

    async def scan(self) -> list:
        # No discovery — XVC servers aren't broadcast-discoverable.
        return []
