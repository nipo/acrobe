"""StLinkMemAp — MEM-AP whose wire step is ST-Link's memory command
set rather than direct CSW / TAR / DRW pokes.

ST-Link manages CSW internally so its READMEM / WRITEMEM commands
behave, and rejects direct CSW writes through ``WRITE_DAP_REG``
(status 0x05) on most firmwares. Only the register-op wire step
differs: blob decomposition, lane handling and the whole
:class:`~acrobe.protocol.memory.Interface` surface are inherited from
:class:`MemAp`, so ``mem_read`` / ``mem_write`` work here exactly as
they do on a bit-banged AP.

The firmware's memory commands are block transfers, not single
accesses, so the lowering coalesces each maximal run of same-width,
same-direction, contiguous ops into one command. With the overwide
read decomposition a whole blob read arrives as one contiguous
``Read32`` run and leaves as a single ``READMEM_32BIT``.
"""

from __future__ import annotations

import struct

from ...component.arm.mem_ap import MemAp
from ...protocol.memory import (
    BackgroundLowering, Read8, Read16, Read32, Write8, Write16, Write32,
)


class StLinkMemAp(MemAp, BackgroundLowering):
    """MEM-AP backed by ST-Link's bulk memory commands."""

    # Word-sized transfers are capped by the firmware's own buffer;
    # stay well inside it and let the run splitter chunk.
    MAX_WORDS_PER_COMMAND = 1024
    MAX_BYTES_PER_COMMAND = 64

    __READ_OPS = (Read8, Read16, Read32)
    __WIDTH = {
        Read8: 1, Write8: 1,
        Read16: 2, Write16: 2,
        Read32: 4, Write32: 4,
    }

    def __init__(self, dp, base: int, idr: int = 0,
                 name: str | None = None):
        super().__init__(dp=dp, base=base, idr=idr, name=name)
        self.__transport = dp._transport
        self.__ap_num = (base >> 24) & 0xFF

    def lower_register_ops(self, batch):
        self.dispatch(batch)

    async def run_ops(self, batch):
        for run in self.__runs(batch):
            await self.__transfer(run)

    @classmethod
    def __runs(cls, batch):
        """Split the batch into maximal runs of ops sharing a width
        and a direction, at ascending contiguous addresses, capped at
        one command's worth of payload."""
        runs: list[list] = []
        current: list = []
        for entry in batch:
            op = entry[0]
            if type(op) not in cls.__WIDTH:
                runs.append([entry])
                current = []
                continue
            if current and cls.__extends(current, op):
                current.append(entry)
                continue
            current = [entry]
            runs.append(current)
        return runs

    @classmethod
    def __extends(cls, current, op) -> bool:
        head = current[0][0]
        if type(head) is not type(op):
            return False
        width = cls.__WIDTH[type(op)]
        if op.addr != head.addr + width * len(current):
            return False
        limit = (cls.MAX_WORDS_PER_COMMAND if width == 4
                 else cls.MAX_BYTES_PER_COMMAND // width)
        return len(current) < limit

    async def __transfer(self, run):
        op = run[0][0]
        kind = type(op)
        width = self.__WIDTH.get(kind)
        if width is None:
            for entry_op, future in run:
                if future is not None:
                    future.set_exception(TypeError(
                        f"StLinkMemAp can't lower "
                        f"{type(entry_op).__name__}"))
            return
        try:
            if kind in self.__READ_OPS:
                await self.__read_run(run, op.addr, width)
            else:
                await self.__write_run(run, op.addr, width)
        except Exception as exc:
            for _, future in run:
                if future is not None and not future.done():
                    future.set_exception(exc)

    async def __read_run(self, run, addr, width):
        length = width * len(run)
        raw = await self.__read(addr, length, width)
        fmt = {1: "<B", 2: "<H", 4: "<I"}[width]
        for index, (_, future) in enumerate(run):
            if future is None:
                continue
            future.set_result(
                struct.unpack_from(fmt, raw, index * width)[0])

    async def __write_run(self, run, addr, width):
        fmt = {1: "<B", 2: "<H", 4: "<I"}[width]
        payload = b"".join(
            struct.pack(fmt, op.data & ((1 << (width * 8)) - 1))
            for op, _ in run)
        await self.__write(addr, payload, width)
        for _, future in run:
            if future is not None:
                future.set_result(None)

    async def __read(self, addr, length, width):
        if width == 4:
            return await self.__transport.read_mem32(
                self.__ap_num, addr, length // 4)
        if width == 2:
            return await self.__transport.read_mem16(
                self.__ap_num, addr, length)
        return await self.__transport.read_mem8(
            self.__ap_num, addr, length)

    async def __write(self, addr, payload, width):
        if width == 4:
            await self.__transport.write_mem32(
                self.__ap_num, addr, payload)
        elif width == 2:
            await self.__transport.write_mem16(
                self.__ap_num, addr, payload)
        else:
            await self.__transport.write_mem8(
                self.__ap_num, addr, payload)
