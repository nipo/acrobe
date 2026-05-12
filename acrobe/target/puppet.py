"""Puppet — trampoline-based remote code execution on one Core.

Skeleton. Real implementation lands in Slice 2 (ArmMPuppet,
AutoPuppet, PuppetStub). The shape sketched here is what flash
regions and provisioning flows will hold a reference to.

A `Puppet` is a Node child of a Target — one Puppet per Core
willing to host stub code. AMP systems may carry multiple Puppets,
each bound to a different Core and Ram. Regions that need
on-target driver code (PuppetFlash) hold a reference to the
appropriate Puppet at construction time, not back through the
Target.
"""

from ..allocator import Allocator
from ..node import Node


class Zone:
    """A slice of target RAM allocated to a puppet caller."""

    def __init__(self, puppet, range):
        self.puppet = puppet
        self.range = range

    @property
    def address(self):
        return self.range.address

    @property
    def end(self):
        return self.range.end

    @property
    def size(self):
        return self.range.size

    async def write(self, data, offset=0):
        if offset + len(data) > self.size:
            raise ValueError("Data does not fit in zone")
        await self.puppet.mem_write(self.address + offset, data)

    async def read(self, size, offset=0):
        if offset + size > self.size:
            raise ValueError("Range does not fit in zone")
        return await self.puppet.mem_read(self.address + offset, size)


class Puppet(Node):
    """Remote-code-exec capability bound to one Core and one Ram.

    Skeleton — concrete subclasses (`ArmMPuppet`, `RvPuppet`)
    arrive in Slice 2+. The constructor signature is the framework
    expectation; CPU-specific subclasses fill in `trampoline_code`
    and the register-name conventions.
    """

    def __init__(self, name, core, ram, *,
                 pc_reg, sp_reg, arg_regs,
                 trampoline_code, stack_size=128, stack_direction=-1):
        super().__init__(name)
        self.core = core
        self.ram = ram
        self.pc_reg = pc_reg
        self.sp_reg = sp_reg
        self.arg_regs = arg_regs
        self.trampoline_code = trampoline_code
        self.stack_size = stack_size
        self.stack_direction = stack_direction
        self.allocator = Allocator(ram.address, ram.size)

    def allocate(self, size, align=1) -> Zone:
        return Zone(self, self.allocator.allocate(size, align))

    def unallocate(self, zone: Zone):
        self.allocator.free(zone.range)

    async def mem_read(self, addr, size):
        raise NotImplementedError

    async def mem_write(self, addr, data):
        raise NotImplementedError

    async def prepare(self, pc, *args):
        raise NotImplementedError

    async def run(self):
        raise NotImplementedError

    async def wait(self, timeout=None):
        raise NotImplementedError

    async def call(self, pc, *args, timeout=None):
        raise NotImplementedError
