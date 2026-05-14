"""Puppet — trampoline-based remote code execution on one Core.

A `Puppet` is a Node child of a Target — one Puppet per Core
willing to host stub code. AMP systems may carry multiple Puppets,
each bound to a different Core and Ram. Regions that need
on-target driver code (PuppetFlash) hold a reference to the
appropriate Puppet at construction time, not back through the
Target.

The host-target ABI lives in `ArmMPuppet`'s `TRAMPOLINE_CODE`: a
12-byte block (8 bytes of code + 4 bytes of function pointer)
loaded into target RAM at puppet construction. `prepare(pc, *args)`
patches the function pointer, sets PC to the trampoline, SP to the
top of an allocated stack, and r0..r3 to the call's arguments.
`run()` resumes the CPU with interrupts masked; the trampoline's
`blx` jumps into the stub, the stub returns via `bx lr`, control
falls back into the trampoline's `bkpt #0xbe`, the core halts, and
`wait()` reads r0 as the return value.

`PuppetStub` wraps an installed-once / call-many-times pattern:
allocate a Zone in RAM, copy the stub bytes there at install time,
re-arm the trampoline pointer per call. Stubs cleanup their zone
on `cleanup()`.

This module deliberately does not concern itself with stub source
or build pipeline — those live alongside the target (e.g. EFM32
ships its compiled `flash_erase` / `flash_write` blobs as bytes
literals in `acrobe.target.arm.efm32`). The puppet just runs
whatever bytes the caller hands it.
"""

from __future__ import annotations

import asyncio
import struct

from ..allocator import Allocator
from ..node import Node
from .debuggable import CoreState


class Zone:
    """A slice of target RAM reserved by a puppet caller.

    Bookkeeping only — the underlying allocator owns the range.
    `Zone.write` / `Zone.read` issue Mem-AP transactions through
    the parent puppet.
    """

    def __init__(self, puppet, range_):
        self.puppet = puppet
        self.range = range_

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
            raise ValueError(
                f"data of {len(data)} bytes does not fit in "
                f"zone of {self.size} bytes")
        await self.puppet.mem_write(self.address + offset, data)

    async def read(self, size, offset=0):
        if offset + size > self.size:
            raise ValueError(
                f"read of {size} bytes does not fit in zone "
                f"of {self.size} bytes")
        return await self.puppet.mem_read(self.address + offset, size)


class PuppetStub:
    """An installed stub-code blob, callable as a function.

    Owns a Zone for the code bytes. `install()` writes the code
    once; `call()` arms the trampoline with the code's entry
    point and runs the puppet. `cleanup()` returns the zone to
    the allocator.
    """

    def __init__(self, puppet, code: bytes, *, name: str = "stub"):
        self.puppet = puppet
        self.code = bytes(code)
        self.name = name
        self.zone = puppet.allocate(len(self.code), align=4)
        self.installed = False

    async def install(self):
        if self.installed:
            return
        await self.zone.write(self.code)
        self.installed = True

    async def call(self, *args, timeout: float = 1.0):
        await self.install()
        # +1 sets the Thumb mode bit consumed by `blx r4` inside
        # the trampoline.
        return await self.puppet.call(self.zone.address | 1, *args,
                                      timeout=timeout)

    async def prepare(self, *args):
        await self.install()
        await self.puppet.prepare(self.zone.address | 1, *args)

    async def run(self):
        await self.puppet.run()

    async def wait(self, timeout: float = 1.0):
        return await self.puppet.wait(timeout=timeout)

    def cleanup(self):
        if self.zone is None:
            return
        self.puppet.unallocate(self.zone)
        self.zone = None
        self.installed = False


class PagedPuppetWriter:
    """Two-buffer pipelined driver for stubs of shape
    ``(dst, src_buf, byte_count)``.

    Allocates two RAM buffers (one if the puppet's allocator can't
    fit a second) and overlaps the host-to-target upload of page N+1
    with the on-target flash burn of page N. The pipeline relies on
    the implicit parallelism between the host's SWD wire (busy
    uploading the next page) and the target's flash controller
    (busy burning the previous page) — no concurrent SWD polling, so
    the bit-bang adapter never sees a mixed read/write stream.

    On chips too tight for two buffers (`Allocator.allocate` raises
    `ValueError` on the second `puppet.allocate`), falls back to the
    synchronous one-buffer pattern — same correctness, no speedup.
    """

    def __init__(self, stub: "PuppetStub", page_size: int, *,
                 timeout: float = 1.0):
        self.stub = stub
        self.puppet = stub.puppet
        self.page_size = page_size
        self.timeout = timeout
        # `free_buf` is the buffer the host writes into next; once a
        # stub starts on it, it becomes `busy_buf` and the previous
        # busy buffer becomes free.
        self.free_buf: Zone | None = None
        self.busy_buf: Zone | None = None
        # True between a successful `stub.run()` and the matching
        # `stub.wait()`. Lets us defer the wait until the start of
        # the next `write()` so the upload runs *before* the wait,
        # giving the target time to burn during the upload.
        self.has_pending = False

    async def __aenter__(self):
        self.free_buf = self.puppet.allocate(self.page_size, align=4)
        try:
            self.busy_buf = self.puppet.allocate(self.page_size, align=4)
        except ValueError:
            self.busy_buf = None
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.has_pending:
                if exc_type is None:
                    await self.stub.wait(timeout=self.timeout)
                self.has_pending = False
        finally:
            if self.free_buf is not None:
                self.puppet.unallocate(self.free_buf)
                self.free_buf = None
            if self.busy_buf is not None:
                self.puppet.unallocate(self.busy_buf)
                self.busy_buf = None

    async def write(self, dst: int, data: bytes):
        """Pipeline one chunk to ``dst``. Returns once the chunk's
        stub is started on the target; the wait happens at the top
        of the next ``write()`` (after that call's upload, so the
        burn and upload truly overlap on the wire) or in
        ``__aexit__``."""
        if self.busy_buf is None:
            # Single-buffer fallback — no pipelining possible.
            await self.free_buf.write(data)
            await self.stub.call(dst, self.free_buf.address, len(data),
                                 timeout=self.timeout)
            return

        # Upload first: SWD is busy with this transfer (~50 ms for a
        # 2 KiB page at 1 MHz), and during that window the target
        # CPU is finishing the previous page's flash burn. No SWD
        # polling races the upload because we save the wait for
        # afterwards.
        await self.free_buf.write(data)

        # Drain the previous stub. By now the burn is almost
        # certainly complete (upload was longer than the burn for
        # every chip we've shipped); this is a quick S_HALT check.
        if self.has_pending:
            await self.stub.wait(timeout=self.timeout)
            self.has_pending = False

        # Launch the next stub on the just-uploaded buffer, then
        # swap so the now-idle buffer is what the next call uploads
        # into.
        await self.stub.prepare(dst, self.free_buf.address, len(data))
        await self.stub.run()
        self.has_pending = True
        self.free_buf, self.busy_buf = self.busy_buf, self.free_buf


class Puppet(Node):
    """Remote-code-exec capability bound to one Core and one Ram.

    Concrete subclasses (`ArmMPuppet`) fill in `TRAMPOLINE_CODE`
    and the register-name conventions. The base class holds the
    Core / Ram references and the allocator.
    """

    TRAMPOLINE_CODE: bytes = b""

    pc_reg: str = "pc"
    sp_reg: str = "sp"
    arg_regs: tuple[str, ...] = ()

    # Bytes appended to TRAMPOLINE_CODE per prepare() to encode the
    # function-pointer literal the trampoline loads.
    PC_SLOT_SIZE = 4

    # Bytes of stack to allocate at construction.
    STACK_SIZE = 256

    def __init__(self, name, core, ram, mem_ap):
        super().__init__(name)
        self.core = core
        self.ram = ram
        self.mem_ap = mem_ap
        self.allocator = Allocator(ram.address, ram.size)
        # Persistent zones: stack + trampoline. Allocated once at
        # construction; freed when the Puppet is dropped.
        self.stack = self.allocate(self.STACK_SIZE, align=8)
        self.trampoline = self.allocate(
            len(self.TRAMPOLINE_CODE) + self.PC_SLOT_SIZE, align=4)

    def allocate(self, size, align=1) -> Zone:
        return Zone(self, self.allocator.allocate(size, align))

    def unallocate(self, zone: Zone):
        self.allocator.free(zone.range)

    async def mem_read(self, addr, size):
        return await self.mem_ap.mem_read(addr, size)

    async def mem_write(self, addr, data):
        await self.mem_ap.mem_write(addr, data)

    def stub(self, code: bytes, *, name: str = "stub") -> PuppetStub:
        return PuppetStub(self, code, name=name)

    async def prepare(self, pc: int, *args):
        raise NotImplementedError

    async def run(self):
        raise NotImplementedError

    async def wait(self, timeout: float = 1.0):
        raise NotImplementedError

    async def call(self, pc: int, *args, timeout: float = 1.0):
        await self.prepare(pc, *args)
        await self.run()
        return await self.wait(timeout=timeout)


class ArmMPuppet(Puppet):
    """Cortex-M puppet.

    Trampoline layout (Thumb, 12 bytes):

        +0  4c01   ldr  r4, [pc, #4]   ; load function pointer
        +2  47a0   blx  r4             ; call into the stub
        +4  bebe   bkpt #0xbe          ; halt on return
        +6  bebe   bkpt #0xbe          ; (padding for word alignment)
        +8  ....   .word  <fnptr>      ; written by prepare()

    `blx r4` returns to `+4` via LR; the bkpt halts the core and
    `wait()` observes `S_HALT`.

    Stack is allocated at construction; the top-of-stack minus 8
    bytes is used as the initial SP (preserves a small landing
    pad for the AAPCS prologue and the trampoline's BLX-link).
    """

    TRAMPOLINE_CODE = b'\x01\x4c\xa0\x47\xbe\xbe\xbe\xbe'

    arg_regs = ("r0", "r1", "r2", "r3")

    POLL_PERIOD = 0.001

    # xPSR with the T (Thumb-state) bit set. Written on every
    # prepare() to guarantee the trampoline executes in Thumb mode
    # regardless of whatever state the CPU was halted in.
    XPSR_THUMB = 0x01000000

    async def prepare(self, pc: int, *args):
        if len(args) > len(self.arg_regs):
            raise ValueError(
                f"too many args: {len(args)} > {len(self.arg_regs)}")
        trampoline_blob = (
            self.TRAMPOLINE_CODE + struct.pack("<I", pc))
        regs = {
            self.sp_reg: self.stack.end - 8,
            self.pc_reg: self.trampoline.address,
            "xpsr": self.XPSR_THUMB,
        }
        for reg, value in zip(self.arg_regs, args):
            regs[reg] = value
        # Fire both ops without intermediate await — same engine
        # queue, so they collapse into a single flush + one swd_io
        # round-trip instead of two.
        f_tramp = self.mem_ap.mem_write(
            self.trampoline.address, trampoline_blob)
        f_regs = self.core.reg_write(regs)
        await asyncio.gather(f_tramp, f_regs)

    async def run(self):
        # Skip pre-resume state check — flash programming halts the
        # core in `pre_program`, every stub call thereafter starts
        # from HALT (the previous stub returned via trampoline bkpt).
        await self.core.resume(allow_interrupts=False)

    async def wait(self, timeout: float = 1.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            state = await self.core.state()
            if state != CoreState.RUN:
                break
            if loop.time() > deadline:
                await self.core.halt()
                raise TimeoutError(
                    f"puppet stub did not return in {timeout}s")
            await asyncio.sleep(self.POLL_PERIOD)
        r0 = self.core.lookup_register(self.arg_regs[0])
        values = await self.core.reg_read([r0])
        return values[r0]
