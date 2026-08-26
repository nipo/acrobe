"""Adapter enumerator for AJI client paths.

Plugs into :func:`acrobe.adapter.model.make_hw_root`. The single
matched name is ``"aji"``: anything else gets passed through to the
next enumerator. The broker handles the rest of the path.
"""

from ..model import Enumerator, enumerator_db
from .broker import AjiBroker


class AjiEnumerator(Enumerator):
    """Attaches the single :class:`AjiBroker` namespace node. AJI
    servers aren't broadcast-discoverable, so there's nothing to scan
    — the broker resolves `aji/<host>/...` on demand."""

    async def populate(self, hw_root):
        if not hw_root.has_child("aji"):
            hw_root.child_add(AjiBroker())


enumerator_db.register("aji")(AjiEnumerator)
