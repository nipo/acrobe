"""Puppet: execute code on the target CPU.

Allocates RAM for stack, trampoline, and data buffers. Calls
functions on the target via a trampoline instruction sequence.
"""

import asyncio
import struct

from .allocator import Allocator
from .node import Node
from .component.arm.cortex import CpuState, CortexReg


class Zone:
    """Allocated RAM range with bus access via MemAp."""

    def __init__(self, mem_ap, alloc_range):
        self.range = alloc_range
        self._ap = mem_ap

    @property
    def address(self):
        return self.range.address

    @property
    def end(self):
        return self.range.end

    @property
    def size(self):
        return self.range.size

    async def read(self, size, offset=0):
        return await self._ap.mem_read(self.address + offset, size)

    async def write(self, data, offset=0):
        await self._ap.mem_write(self.address + offset, data)


class PuppetStub:
    """Code zone wrapper for a compiled stub.

    Manages allocation/lifecycle of the code zone and provides
    call/prepare/run/wait interface.
    """

    def __init__(self, puppet, code):
        self.puppet = puppet
        self.code = code
        self.zone = self.puppet.allocate(len(code))
        self._cleaned = False

    async def call(self, *args, timeout=None):
        await self.zone.write(self.code)
        return await self.puppet.call(self.zone.address + 1, *args, timeout=timeout)

    async def prepare(self, *args):
        await self.zone.write(self.code)
        await self.puppet.prepare(self.zone.address + 1, *args)

    async def run(self):
        await self.puppet.run()

    async def wait(self, timeout=None):
        return await self.puppet.wait(timeout=timeout)

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        if self.zone:
            self.puppet.free(self.zone)
            self.zone = None


class Puppet(Node):
    """Execute code on the target CPU.

    Uses a trampoline instruction sequence to call arbitrary functions.
    Arguments passed in CPU registers (r0-r3 on ARM), return value in r0.

    Args:
        cpu: Cortex instance
        mem_ap: MemAp for memory access
        ram_address: base address of usable RAM
        ram_size: size of usable RAM
        trampoline_code: machine code for the trampoline (without function pointer)
        pc_reg: CPU register number for PC
        sp_reg: CPU register number for SP
        arg_regs: list of register numbers for arguments
        stack_size: stack allocation size
    """

    def __init__(self, cpu, mem_ap, ram_address, ram_size,
                 trampoline_code, *,
                 pc_reg=CortexReg.PC, sp_reg=CortexReg.SP,
                 arg_regs=(CortexReg.R0, CortexReg.R1,
                           CortexReg.R2, CortexReg.R3),
                 stack_size=128):
        super().__init__("puppet")
        self.cpu = cpu
        self._ap = mem_ap
        self._allocator = Allocator(ram_address, ram_size)
        self._pc_reg = pc_reg
        self._sp_reg = sp_reg
        self._arg_regs = list(arg_regs)
        self._trampoline_code = trampoline_code
        self._stack = self.allocate(stack_size)
        self._stack_init = self._stack.end - 8  # Full descending stack
        self._trampoline = self.allocate(len(trampoline_code) + 4)

    def allocate(self, size, align=1):
        """Allocate a Zone from RAM."""
        r = self._allocator.allocate(size, align)
        return Zone(self._ap, r)

    def free(self, zone):
        """Free a Zone back to the allocator."""
        self._allocator.free(zone.range)

    async def prepare(self, pc, *args):
        """Set up trampoline, stack, and arguments for a function call."""
        assert len(args) <= len(self._arg_regs)

        regs = {
            self._sp_reg: self._stack_init,
            self._pc_reg: self._trampoline.address,
        }
        for reg, val in zip(self._arg_regs, args):
            regs[reg] = val

        # Write trampoline code + function pointer
        tc = self._trampoline_code + struct.pack("<I", pc)
        await self._trampoline.write(tc)

        # Set CPU registers
        await self.cpu.regs_write(regs)

    async def run(self):
        """Resume CPU execution."""
        await self.cpu.resume()

    async def wait(self, timeout=None):
        """Poll for CPU halt with async sleep.

        Returns the value of r0 (first argument register).
        """
        timeout = timeout or 0.2
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            state = await self.cpu.state()
            if state == CpuState.HALT:
                break
            if asyncio.get_event_loop().time() >= deadline:
                await self.cpu.halt()
                self.logger.error("Forced stop of target (timeout)")
                break
            await asyncio.sleep(0.001)

        return await self.cpu.reg_read(self._arg_regs[0])

    async def call(self, pc, *args, timeout=None):
        """Prepare, run, and wait for a function call. Returns r0."""
        await self.prepare(pc, *args)
        await self.run()
        return await self.wait(timeout=timeout)

    def stub(self, code):
        """Create a PuppetStub for a compiled code blob."""
        return PuppetStub(self, code)


# --- ARM Cortex-M Puppet ---

# Thumb-2 trampoline: ldr r12, [pc, #0]; bx r12
# Followed by 4-byte function pointer
ARM_M_TRAMPOLINE = b'\x01L\xa0\x47\xbe\xbe\xbe\xbe'

# CRC32 stub machine code (Cortex-M0 compatible)
# memory_crc32(address, size) -> CRC32 in r0
CRC32_CODE = bytes([
    0x30, 0xb5,              # push {r4, r5, lr}
    0x09, 0x1a,              # subs r1, r1, r0
    0x04, 0x46,              # mov r4, r0
    0x4f, 0xf0, 0xff, 0x30,  # mov r0, #0xffffffff
    0x0d, 0xe0,              # b loop_check
    # loop_body:
    0x65, 0x5d,              # ldrb r5, [r4, r1]
    0x45, 0x40,              # eors r5, r0
    0x07, 0x25,              # movs r5, #7 (actually: 8 iterations)
    # bit_loop:
    0xc3, 0x17,              # asrs r3, r0, #31
    0x40, 0x00,              # lsls r0, r0, #1
    0x4b, 0x4b,              # ldr r3, [pc, #poly]
    0x03, 0x40,              # ands r3, r0
    0x18, 0x46,              # mov r0, r3
    0x01, 0x3d,              # subs r5, #1
    0xf8, 0xd5,              # bpl bit_loop
    # loop_check:
    0x01, 0x39,              # subs r1, #1
    0xf2, 0xd5,              # bpl loop_body
    0x30, 0xbd,              # pop {r4, r5, pc}
    # polynomial:
    0x01, 0x00, 0x00, 0xed,  # .word 0xed000001 (reversed CRC32 poly)
])

# CRC32 many: memory_crc32_many(params_address, count)
# params is array of (address, size) pairs, CRC written back into address field
CRC32_MANY_CODE = bytes([
    0x70, 0xb5,              # push {r4, r5, r6, lr}
    0x04, 0x46,              # mov r4, r0  (params ptr)
    0x0d, 0x46,              # mov r5, r1  (count)
    0x00, 0x2d,              # cmp r5, #0
    0x08, 0xdd,              # ble done
    # loop:
    0x20, 0x68,              # ldr r0, [r4, #0]  (address)
    0x61, 0x68,              # ldr r1, [r4, #4]  (size)
    0x00, 0xf0, 0x00, 0xf8,  # bl crc32 (relative, patched at runtime)
    0x20, 0x60,              # str r0, [r4, #0]  (write CRC back)
    0x08, 0x34,              # adds r4, #8
    0x01, 0x3d,              # subs r5, #1
    0xf7, 0xd1,              # bne loop
    # done:
    0x70, 0xbd,              # pop {r4, r5, r6, pc}
])


class ArmMPuppet(Puppet):
    """ARM Cortex-M puppet with CRC32 support."""

    def __init__(self, cpu, mem_ap, ram_address, ram_size):
        super().__init__(
            cpu, mem_ap, ram_address, ram_size,
            ARM_M_TRAMPOLINE,
        )

    async def crc32(self, address, size):
        """Compute CRC32 of a memory range on the target."""
        code = self.stub(CRC32_CODE)
        try:
            return await code.call(address, size)
        finally:
            code.cleanup()

    async def crc32_many(self, ranges):
        """Compute CRC32 of multiple memory ranges.

        Args:
            ranges: list of (address, size) tuples

        Returns:
            dict mapping address -> CRC32 value
        """
        if not ranges:
            return {}

        code = self.stub(CRC32_MANY_CODE)
        try:
            params = self.allocate(len(ranges) * 8)
            try:
                # Pack params: (address, size) pairs
                blob = b''.join(
                    struct.pack("<II", addr, sz)
                    for addr, sz in sorted(ranges)
                )
                await params.write(blob)
                await code.call(params.address, len(ranges))

                # Read back: CRC replaces address field
                response = await params.read(len(blob))
            finally:
                self.free(params)

            values = {}
            for i, (addr, _) in enumerate(sorted(ranges)):
                values[addr] = struct.unpack_from("<I", response, i * 8)[0]
            return values
        finally:
            code.cleanup()
