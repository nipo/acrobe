"""AJI client adapter.

Lets acrobe consume a remote AJI server (typically Quartus' ``jtagd``)
as a JTAG transport. Path layout under HwRoot:

    aji/<host>[:port]/<hardware>/<tap>

where ``<hardware>`` is one cable or board the AJI server has
registered (mirrors libaji's ``Hardware`` record), and ``<tap>`` is a
single TAP on that hardware's chain (named ``tap0``, ``tap1``, …).

Built on the in-tree :mod:`.client` / :mod:`.link` / :mod:`.wire`
modules, which speak the protocol Intel publishes (see
``libaji_client/src/jtag/``) — ported here from a libaji-faithful
reimplementation rather than reused from any external package.
"""

from .broker import AjiBroker
from .enumerator import AjiEnumerator
from .hardware import AjiHardware
from .host import AjiHost

__all__ = [
    "AjiBroker",
    "AjiEnumerator",
    "AjiHardware",
    "AjiHost",
]
