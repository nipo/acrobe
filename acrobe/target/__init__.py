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
from .region import Region, Ram, Flash, Eeprom, NotUpdatable
from .debuggable import (
    Debuggable, Core, Register, RegisterType, CoreState, HaltCause,
)
from .puppet import Puppet, Zone
from .debug_auth import DebugAuth

from . import fpga  # noqa: F401,E402  — triggers @Target.register
from . import spi_flash  # noqa: F401,E402
from .arm import cortex_m as _cortex_m  # noqa: F401,E402
from .arm import nrf52 as _nrf52  # noqa: F401,E402

__all__ = [
    "Target", "Explorer", "TargetDiscovery",
    "Loadable",
    "Region", "Ram", "Flash", "Eeprom", "NotUpdatable",
    "Debuggable", "Core", "Register", "RegisterType",
    "CoreState", "HaltCause",
    "Puppet", "Zone",
    "DebugAuth",
]
