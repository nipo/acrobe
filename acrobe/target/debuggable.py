"""Debuggable — run-control view on a Target.

Skeleton. Implementations land in Slice 2 (Cortex-M) and Slice 3
(ARM9, RISC-V). The shape here is what the GDB binding and CLI
debug commands will consume.

CPU-family agnostic surface: `mem_read` / `mem_write` are methods on
`Debuggable`, not on a component-side Bus. Subclasses implement
memory access in whatever way their family supports — Mem-AP for
Cortex-M, instruction stuffing on EmbeddedICE for ARM9, Debug
Module system-bus or program-buffer for RISC-V.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from ..node import Node


class RegisterType(Enum):
    GPR = 0
    FLOAT = 1
    DOUBLE = 2
    PC = 3
    LR = 4
    SP = 5
    SYSTEM = 6


@dataclass(frozen=True)
class Register:
    """Architectural register description.

    `number`      — architectural/family-defined register number.
    `name`        — debugger-facing name.
    `width`       — bit width.
    `datatype`    — `RegisterType` value.
    `group`       — coarse register group ("general", "system", …).
    `gdb_visible` — whether GDB target.xml exposes this register.
    """

    number: int
    name: str
    width: int
    datatype: RegisterType
    group: str
    gdb_visible: bool = True

    def __lt__(self, other):
        return self.number < other.number


class CoreState(Enum):
    RUN = 0
    HALT = 1
    SLEEP = 2
    FAULT = 3
    LOCKUP = 4
    UNKNOWN = 5


class HaltCause(Enum):
    EXCEPTION = 0
    INSTRUCTION = 1
    BREAKPOINT = 2
    WATCHPOINT = 3
    DEBUGGER = 4
    UNKNOWN = 5


class Core(Node):
    """One thread of execution. Self-describes its GDB feature set.

    Skeleton — concrete subclasses (CortexMCore, ARM9Core, RvHart)
    arrive in Slice 2+.
    """

    gdb_feature_name: str = ""
    gdb_byteorder: Literal["little", "big"] = "little"

    def __init__(self, name):
        super().__init__(name)
        self.registers: list[Register] = []

    async def state(self) -> CoreState:
        raise NotImplementedError

    async def halt_cause(self) -> HaltCause:
        raise NotImplementedError

    async def halt(self):
        raise NotImplementedError

    async def resume(self, *, allow_interrupts=True):
        raise NotImplementedError

    async def step(self):
        raise NotImplementedError

    async def reset(self, *, stop=True):
        raise NotImplementedError

    async def reg_read(self, regs):
        raise NotImplementedError

    async def reg_write(self, reg_values):
        raise NotImplementedError

    async def breakpoint_add(self, addr, kind):
        raise NotImplementedError

    async def breakpoint_remove(self, bp):
        raise NotImplementedError

    async def breakpoint_list(self):
        return []

    async def watchpoint_add(self, addr, size, kind):
        raise NotImplementedError

    async def watchpoint_remove(self, wp):
        raise NotImplementedError

    async def watchpoint_list(self):
        return []


class Debuggable(Node):
    """Run-control + memory access on a Target.

    Skeleton. Subclasses hold component references (Mem-AP, EICE,
    Debug Module) and concretise `mem_read` / `mem_write` and
    `attach` / `detach`. `cores` are Node children under this
    Debuggable; `memory_map` is the list of Regions the debug view
    exposes to GDB (may overlap a sibling Loadable's regions).
    """

    def __init__(self, name="debug"):
        super().__init__(name)
        self.memory_map = []

    @property
    def cores(self):
        return self.children_of_class(Core)

    @property
    def flash_route(self):
        """Loadable to route GDB's vFlashErase/Write into.

        Default: the first Loadable sibling under the same Target.
        Subclasses may override to pick a specific Loadable when
        the Target carries several.
        """
        from .loadable import Loadable
        siblings = self._parent.children_of_class(Loadable) if self._parent else []
        return siblings[0] if siblings else None

    async def attach(self):
        from .debug_auth import DebugAuth
        auths = self._parent.children_of_class(DebugAuth) if self._parent else []
        for auth in auths:
            await auth.authorize(self)

    async def detach(self):
        raise NotImplementedError

    async def mem_read(self, addr, size):
        raise NotImplementedError

    async def mem_write(self, addr, data):
        raise NotImplementedError

    async def monitor(self, cmd, args):
        raise NotImplementedError
