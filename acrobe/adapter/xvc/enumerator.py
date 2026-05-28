"""Adapter enumerator for XVC client paths.

Plugs into :func:`acrobe.adapter.model.make_hw_root`. The single
matched name is ``"xvc"``: anything else gets passed through to the
next enumerator. The broker handles the rest of the path.
"""

from ..model import Enumerator, enumerator_db
from .broker import XvcBroker


class XvcEnumerator(Enumerator):
    """Attaches the single :class:`XvcBroker` namespace node. XVC
    servers aren't broadcast-discoverable; the broker resolves
    `xvc/<host>[:port]/<chain>/...` on demand."""

    async def populate(self, hw_root):
        if not hw_root.has_child("xvc"):
            hw_root.child_add(XvcBroker())


enumerator_db.register("xvc")(XvcEnumerator)
