"""XVC client adapter — talk to a remote Xilinx Virtual Cable server
as if it were a local JTAG interface.

Resolved through :func:`acrobe.adapter.model.make_hw_root` under the
literal name ``xvc``: a path like ``xvc/<host>[:<port>]/<chain>/...``
spawns an :class:`XvcBroker` → :class:`XvcClient` → users layer their
own :class:`Chain`/:class:`Tap` on top.
"""

from .enumerator import XvcEnumerator
from .broker import XvcBroker
from .client import XvcClient

__all__ = ["XvcEnumerator", "XvcBroker", "XvcClient"]
