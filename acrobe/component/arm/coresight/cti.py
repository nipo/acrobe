"""Cross-Trigger Interface (CTI). DEVTYPE = 0x14 (Debug control,
Trigger Matrix). Routes debug events between CoreSight components
(halt-on-trace-trigger, run-on-cross-debug-stop, etc.).

Trigger inputs and outputs are wired to the attached component
(debug request, restart, trace triggers, ...), channels are shared
with every other CTI through the Cross Trigger Matrix. Which trigger
is which signal is a SoC integration fact, the CTI itself can't tell.

DEVARCH ARCHID = 0x1A14 identifies CTIv2 on ARMv8."""

from ....bitfield import Bitfield, BooleanField, Field
from .model import CoresightComponent, DevArch, MemoryMappedComponent


class CtiDevId(Bitfield):
    triggers = Field(8, 8)
    channels = Field(16, 4)


class CoresightLockStatus(Bitfield):
    implemented = BooleanField(0)
    locked = BooleanField(1)


@CoresightComponent.db.register(0x14)
@MemoryMappedComponent.devarch_db.register(DevArch(architect=0x23B, archid=0x1A14, revision=0, present=True))
class Cti(CoresightComponent):
    FRIENDLY_NAME = "Cross-Trigger Interface"

    CONTROL = 0x000
    INTACK = 0x010
    APPSET = 0x014
    APPCLEAR = 0x018
    APPPULSE = 0x01c
    INEN = 0x020
    OUTEN = 0x0a0
    TRIGINSTATUS = 0x130
    TRIGOUTSTATUS = 0x134
    CHINSTATUS = 0x138
    CHOUTSTATUS = 0x13c
    GATE = 0x140
    LAR = 0xfb0
    LSR = 0xfb4

    LAR_KEY = 0xc5acce55
    CONTROL_GLBEN = 1 << 0

    def geometry(self) -> CtiDevId:
        return CtiDevId(self.devid or 0)

    async def unlock(self):
        """Lift the software lock, which only applies to accesses
        from the system side but is harmless otherwise."""
        if CoresightLockStatus(await self.reg_read(self.LSR)).locked:
            await self.reg_write(self.LAR, self.LAR_KEY)

    async def enable(self, enabled: bool = True):
        await self.reg_write(self.CONTROL, self.CONTROL_GLBEN if enabled else 0)

    async def trigger_in_route(self, trigger: int, channels: int):
        """Channels mask `trigger` input raises an event on."""
        await self.reg_write(self.INEN + 4 * trigger, channels)

    async def trigger_out_route(self, trigger: int, channels: int):
        """Channels mask whose events assert `trigger` output."""
        await self.reg_write(self.OUTEN + 4 * trigger, channels)

    async def gate(self, channels: int):
        """Channels mask this CTI propagates to the matrix."""
        await self.reg_write(self.GATE, channels)

    async def pulse(self, channels: int):
        await self.reg_write(self.APPPULSE, channels)

    async def channel_set(self, channels: int):
        await self.reg_write(self.APPSET, channels)

    async def channel_clear(self, channels: int):
        await self.reg_write(self.APPCLEAR, channels)

    async def acknowledge(self, triggers: int):
        """Deassert latched trigger outputs that have no hardware
        acknowledge, like a level-sensitive debug request."""
        await self.reg_write(self.INTACK, triggers)

    async def trigger_out_status(self) -> int:
        return await self.reg_read(self.TRIGOUTSTATUS)

    async def reset(self):
        """Disable and unroute every trigger."""
        await self.enable(False)
        geometry = self.geometry()
        for trigger in range(geometry.triggers):
            await self.trigger_in_route(trigger, 0)
            await self.trigger_out_route(trigger, 0)
        await self.acknowledge((1 << geometry.triggers) - 1)
