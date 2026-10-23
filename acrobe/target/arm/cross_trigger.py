"""Synchronous run control of several ARMv7-A/R cores through their
CTIs. Each core keeps its own halt and resume; this only makes them
happen on the same cycle.

Trigger wiring follows the ARMv7 debug architecture recommendation
the Cortex-A9 integrations use: CTI trigger output 0 drives the debug
request, output 7 the restart request. Two matrix channels carry the
halt and restart events to every CTI of the group.
"""

from ...component.arm.coresight.cti import Cti
from .cortex_a import CortexACore


class CrossTrigger:
    TRIGGER_DEBUG_REQUEST = 0
    TRIGGER_RESTART = 7
    CHANNEL_HALT = 0
    CHANNEL_RESTART = 1

    def __init__(self, pairs: list[tuple[CortexACore, Cti]]):
        if not pairs:
            raise ValueError("cross trigger group needs cores")
        self.pairs = pairs
        self.__configured = False

    @property
    def channels(self) -> int:
        return 1 << self.CHANNEL_HALT | 1 << self.CHANNEL_RESTART

    async def setup(self):
        for _core, cti in self.pairs:
            await cti.unlock()
            await cti.trigger_out_route(self.TRIGGER_DEBUG_REQUEST,
                                        1 << self.CHANNEL_HALT)
            await cti.trigger_out_route(self.TRIGGER_RESTART,
                                        1 << self.CHANNEL_RESTART)
            await cti.gate(self.channels)
            await cti.enable()
        self.__configured = True

    async def teardown(self):
        if not self.__configured:
            return
        for _core, cti in self.pairs:
            await cti.trigger_out_route(self.TRIGGER_DEBUG_REQUEST, 0)
            await cti.trigger_out_route(self.TRIGGER_RESTART, 0)
            await cti.acknowledge(1 << self.TRIGGER_DEBUG_REQUEST)
        self.__configured = False

    async def __configure(self):
        if not self.__configured:
            await self.setup()

    async def halt(self):
        """Halt every running core at once. A core in WFI or WFE may
        have its clock, and its CTI's, stopped: a pulse would be
        missed, the halt channel is held until every core is halted
        instead. The debug request is level sensitive, it must be
        dropped before the cores can restart."""
        await self.__configure()
        cti = self.pairs[0][1]
        await cti.channel_set(1 << self.CHANNEL_HALT)
        try:
            for core, _cti in self.pairs:
                await core.debug.wait_dscr(lambda d: d.halted)
        finally:
            await cti.channel_clear(1 << self.CHANNEL_HALT)
        for _core, cti in self.pairs:
            await cti.acknowledge(1 << self.TRIGGER_DEBUG_REQUEST)
        for core, _cti in self.pairs:
            await core.halt()

    async def resume(self):
        """Restart every core at once. All of them must be halted,
        so clocked: a pulse reaches them, and unlike a held channel
        can't restart a core that halts again right away."""
        await self.__configure()
        for core, _cti in self.pairs:
            await core.resume_prepare()
            await core.debug.restart_prepare()
        for _core, cti in self.pairs:
            await cti.acknowledge(1 << self.TRIGGER_DEBUG_REQUEST)
        await self.pairs[0][1].pulse(1 << self.CHANNEL_RESTART)
        for core, _cti in self.pairs:
            await core.debug.wait_restarted()
