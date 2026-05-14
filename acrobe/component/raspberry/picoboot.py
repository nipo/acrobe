"""PICOBOOT — RP2040 ROM USB bootloader puppet.

PICOBOOT is the vendor USB interface (VID 0x2e8a, PID 0x0003)
exposed by the RP2040 ROM bootloader when the chip enters BOOTSEL
mode. Its command set covers READ/WRITE of arbitrary memory and
EXEC of a function at a given address — enough to satisfy the
host-side surface of `acrobe.target.puppet.Puppet`.

This module provides:

- `PicobootTransport` — the typing Protocol the puppet expects
  from a USB-level PICOBOOT binding (`read`, `write`, `exec`).
  The actual USB binding lives elsewhere and feeds this puppet.
- `PicobootPuppet` — a `PuppetBase` implementation that runs ARM
  Cortex-M0+ stubs (the RP2040's only ISA) by:
    1. Writing a small thunk to RAM once per puppet lifetime.
    2. Per call, writing the function pointer and up to four
       word arguments into a fixed data area.
    3. Issuing PICOBOOT EXEC at the thunk's entry; the thunk
       loads r0..r3, calls the function, stashes its return
       value, and returns to the bootrom.
    4. Reading the return value back out of the data area.

Trampoline ABI
--------------

Hand-assembled ARMv6-M Thumb code (verified against arm-none-eabi-as
for cortex-m0plus):

    0:  4f04        ldr  r7, [pc, #16]   ; r7 = &data area
    2:  b500        push {lr}            ; save bootrom return
    4:  683c        ldr  r4, [r7, #0]    ; r4 = fn_pc
    6:  6878        ldr  r0, [r7, #4]    ; args[0]
    8:  68b9        ldr  r1, [r7, #8]    ; args[1]
    a:  68fa        ldr  r2, [r7, #12]   ; args[2]
    c:  693b        ldr  r3, [r7, #16]   ; args[3]
    e:  47a0        blx  r4              ; call
   10:  6178        str  r0, [r7, #20]   ; result = r0
   12:  bd00        pop  {pc}            ; return to bootrom
   14:  <data_addr>                      ; literal (patched at install)

The thunk returns normally rather than spinning: PICOBOOT EXEC
runs in USB IRQ context and the bootrom holds the bulk-IN status
ACK until the called function returns. Spinning here would nest
EXECs onto the bootrom's stack on every subsequent call.

Data area layout (24 bytes, separate allocation):

    +0   fn_pc       (Thumb bit set)
    +4   arg0
    +8   arg1
    +12  arg2
    +16  arg3
    +20  result      (written by thunk before pop {pc})

The thunk and data area are independent allocations so they can
be inspected and reasoned about separately. The data_addr
literal in the thunk's tail makes the cross-zone pointer.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Protocol, runtime_checkable

from ...target.puppet import PuppetBase


@runtime_checkable
class PicobootTransport(Protocol):
    """USB-level surface a `PicobootPuppet` consumes.

    Concrete bindings (libusb-backed, mock, replay) implement
    these three methods over the PICOBOOT vendor protocol.
    """

    async def read(self, addr: int, size: int) -> bytes: ...
    async def write(self, addr: int, data: bytes) -> None: ...
    async def exec(self, pc: int) -> None: ...


class PicobootPuppet(PuppetBase):
    """`Puppet` implementation over PICOBOOT.

    Construction takes the puppet's `Ram` region (a slice of
    RP2040 SRAM safe to use — not overlapping the bootrom's own
    workspace) and a `PicobootTransport`. The thunk is installed
    lazily on first `prepare()`.
    """

    THUNK_CODE = bytes.fromhex(
        "044f"  # ldr r7, [pc, #16]
        "00b5"  # push {lr}
        "3c68"  # ldr r4, [r7, #0]
        "6878"  # ldr r0, [r7, #4]
        "b968"  # ldr r1, [r7, #8]
        "fa68"  # ldr r2, [r7, #12]
        "3b69"  # ldr r3, [r7, #16]
        "a047"  # blx r4
        "7861"  # str r0, [r7, #20]
        "00bd"  # pop {pc}
    )

    LITERAL_SIZE = 4
    DATA_SIZE = 24
    DATA_OFFSET_FN = 0
    DATA_OFFSET_ARGS = 4
    DATA_OFFSET_RESULT = 20

    MAX_ARGS = 4

    def __init__(self, name, ram, transport: PicobootTransport):
        super().__init__(name, ram)
        self.transport = transport
        self.trampoline = self.allocate(
            len(self.THUNK_CODE) + self.LITERAL_SIZE, align=4)
        self.data = self.allocate(self.DATA_SIZE, align=4)
        self.__installed = False
        self.__exec_task: asyncio.Task | None = None

    async def __install(self):
        blob = (self.THUNK_CODE
                + struct.pack("<I", self.data.address))
        await self.transport.write(self.trampoline.address, blob)
        self.__installed = True

    async def mem_read(self, addr, size):
        return await self.transport.read(addr, size)

    async def mem_write(self, addr, data):
        await self.transport.write(addr, bytes(data))

    async def prepare(self, pc: int, *args):
        if len(args) > self.MAX_ARGS:
            raise ValueError(
                f"too many args: {len(args)} > {self.MAX_ARGS}")
        if not self.__installed:
            await self.__install()
        padded = list(args) + [0] * (self.MAX_ARGS - len(args))
        # fn_pc (Thumb bit set) | args[0..3] | result_slot (zeroed)
        payload = struct.pack(
            "<6I", pc | 1, *padded, 0)
        await self.transport.write(self.data.address, payload)

    async def run(self):
        if self.__exec_task is not None:
            raise RuntimeError(
                "PicobootPuppet.run() called with a pending exec")
        # Wrap in a Task so the EXEC USB transaction proceeds
        # concurrently with whatever the caller does between
        # run() and wait(). The bootrom serialises commands at
        # the device, so real overlap requires the transport to
        # do so too — at minimum this preserves the puppet API
        # shape `PagedPuppetWriter` expects.
        self.__exec_task = asyncio.create_task(
            self.transport.exec(self.trampoline.address | 1))

    async def wait(self, timeout: float = 1.0):
        if self.__exec_task is None:
            raise RuntimeError(
                "PicobootPuppet.wait() called with no pending exec")
        task = self.__exec_task
        self.__exec_task = None
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"PICOBOOT exec did not return in {timeout}s") from e
        result_bytes = await self.transport.read(
            self.data.address + self.DATA_OFFSET_RESULT, 4)
        return struct.unpack("<I", result_bytes)[0]
