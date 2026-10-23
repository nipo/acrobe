"""Cypress EZ-USB FX3 target.

The FX3 has no on-chip flash: firmware is loaded to RAM and started
from its entry point. Loading is destructive, the core context at
attach time is not preserved.
"""

from __future__ import annotations

from ...component.arm.arm9.debug import Arm9Debug
from ...component.arm.memory import BusRam
from ...component.cypress.fx3 import Fx3Tap
from ...db import NoMatch
from ..loadable import RamLoadable
from ..memory import Memory
from ..region import Ram
from ..target import Target
from .arm9 import Arm926Core, Arm9Debuggable


class Fx3Loadable(RamLoadable):
    """Maps the TCMs and turns caches and MMU off before loading,
    as the boot ROM leaves them in an unspecified state."""

    async def pre_program(self, *, do_erase, assume_clean):
        await super().pre_program(do_erase=do_erase, assume_clean=assume_clean)
        await self.core.caches_disable()
        await self.core.cp15.tcm_enable(True, Fx3Target.ITCM[0])
        await self.core.cp15.tcm_enable(False, Fx3Target.DTCM[0])


@Target.register(Fx3Tap)
class Fx3Target(Target):
    """EZ-USB FX3. SYSMEM is sized for the 512 KiB parts; on
    CYUSB3011 / CYUSB3012 only its first 256 KiB exist."""

    ITCM = (0x00000000, 0x4000)
    DTCM = (0x10000000, 0x2000)
    SYSMEM = (0x40000000, 0x80000)
    MMIO = (0xe0000000, 0x10000000)
    BOOTROM = (0xffff0000, 0x8000)
    VIC = (0xfffff000, 0x1000)

    LOADABLE = ("itcm", "dtcm", "sysmem")

    def __init__(self, tap: Fx3Tap):
        super().__init__(tap.name)
        debugs = tap.children_of_class(Arm9Debug)
        if not debugs:
            raise NoMatch("ARM9 debug", tap.name)
        self.core = Arm926Core("cpu", debugs[0])

        regions = {
            "itcm": self.ITCM, "dtcm": self.DTCM, "sysmem": self.SYSMEM,
            "mmio": self.MMIO, "bootrom": self.BOOTROM, "vic": self.VIC,
        }

        debug = Arm9Debuggable(self.core)
        for name, (address, size) in regions.items():
            debug.memory_map.append(Ram(name, address, size))
        self.child_add(debug)

        memory = Memory(self.core)
        for name, (address, size) in regions.items():
            memory.child_add(BusRam(name, address, size, self.core))
        self.child_add(memory)

        loadable = Fx3Loadable(self.core)
        for name in self.LOADABLE:
            address, size = regions[name]
            loadable.child_add(BusRam(name, address, size, self.core))
        self.child_add(loadable)
