"""Target framework.

Targets are Nodes parented flat under the root, discovered from
the component tree by `TargetDiscovery`. Each Target gathers one
or more view children: `Loadable` (programming), `Debuggable`
(run-control), `Puppet` (trampoline exec), `DebugAuth` (keyed
debug). Views hold direct references to component-tree nodes;
the component tree retains ownership.

See `PLAN_target.md` for the full design.
"""

from .target import Target, Explorer
from .discovery import TargetDiscovery
from .loadable import Loadable
from .memory import Memory
from .region import Region, Ram, Flash, Eeprom, NotUpdatable
from .debuggable import (
    Debuggable, Core, Register, RegisterType, CoreState, HaltCause,
)
from .puppet import (
    Puppet, PuppetBase, ArmMPuppet, PuppetStub, PagedPuppetWriter, Zone,
)
from .debug_auth import DebugAuth

from . import fpga  # noqa: F401,E402  — triggers @Target.register
from . import spi_flash  # noqa: F401,E402
from . import rtt as _rtt  # noqa: F401,E402  — triggers @Ram.db.register
from .arm import cortex_m as _cortex_m  # noqa: F401,E402
from .arm import soc as _arm_soc  # noqa: F401,E402
from .arm import nrf52 as _nrf52  # noqa: F401,E402
from .arm import efm32 as _efm32  # noqa: F401,E402
from .arm import rp2040 as _rp2040  # noqa: F401,E402
from .arm import rp2040_swd as _rp2040_swd  # noqa: F401,E402

__all__ = [
    "Target", "Explorer", "TargetDiscovery",
    "Loadable",
    "Memory",
    "Region", "Ram", "Flash", "Eeprom", "NotUpdatable",
    "Debuggable", "Core", "Register", "RegisterType",
    "CoreState", "HaltCause",
    "Puppet", "PuppetBase", "ArmMPuppet", "PuppetStub",
    "PagedPuppetWriter", "Zone",
    "DebugAuth",
]
