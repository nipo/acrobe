"""DebugAuth — gate for keyed / authenticated debug.

Skeleton. Implementations land in Slice 4 (nRF53 APPROTECT-CTRL-AP,
STM32H5 DBGAUTH, SAM-L11 DSU keyed unlock, RP2350 secure boot,
etc.). The framework's contract: a `Debuggable` calls
`target.children_of_class(DebugAuth)[0].authorize(self)` at the
start of `attach()` if present.

Default `authorize` is a no-op so Targets without auth needs work
without ceremony.
"""

from ..node import Node


class DebugAuth(Node):
    def __init__(self, name="auth"):
        super().__init__(name)

    async def authorize(self, debuggable):
        """Gain access to `debuggable`. Default: pass."""
