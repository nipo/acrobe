"""StLinkMemAp — MemAp variant that uses ST-Link's bulk memory
commands instead of poking CSW/TAR/DRW directly.

ST-Link doesn't allow direct CSW writes through ``WRITE_DAP_REG``
(it manages CSW internally so its READMEM/WRITEMEM commands work
correctly). Honouring the abstraction line our :class:`MemAp`
draws — ``flush_ops`` translates frozen mem-op dataclasses to
backend transactions — we override that single method here to
issue ST-Link commands.

Reads and writes are one USB round-trip per op for now. A future
optimisation can batch contiguous same-size accesses into a single
``read_mem32`` / ``write_mem32`` call (ST-Link supports up to
``STLINK_MAX_RW16_32`` = 6144 bytes per command).
"""

from __future__ import annotations

import asyncio
import struct

from ...component.arm.mem_ap import (
    MemAp, Read8, Read16, Read32, Write8, Write16, Write32,
)


class StLinkMemAp(MemAp):
    """MEM-AP backed by ST-Link's high-level bulk memory commands.

    Skips the inherited CSW/TAR/DRW state machine entirely — ST-Link
    handles CSW internally, and direct CSW writes via WRITE_DAP_REG
    are rejected on most firmwares anyway (status 0x05).

    32-bit accesses use the bulk READMEM_32BIT/WRITEMEM_32BIT
    commands. 8/16-bit accesses are not implemented yet (slice
    follow-up); for now they raise NotImplementedError so callers
    can decide whether to fall back to word-size accesses with
    masking."""

    def __init__(self, dp, base: int, idr: int = 0,
                 name: str | None = None):
        super().__init__(dp=dp, base=base, idr=idr, name=name)
        self._transport = dp._transport
        self._ap_num = (base >> 24) & 0xFF

    async def flush_ops(self, batch):
        for op, future in batch:
            try:
                if isinstance(op, Read32):
                    raw = await self._transport.read_mem32(
                        self._ap_num, op.addr, 1)
                    future.set_result(struct.unpack_from("<I", raw)[0])
                elif isinstance(op, Write32):
                    await self._transport.write_mem32(
                        self._ap_num, op.addr,
                        struct.pack("<I", op.data))
                    future.set_result(None)
                elif isinstance(op, (Read8, Read16, Write8, Write16)):
                    # Slice follow-up: ST-Link has dedicated 8/16-bit
                    # commands. For now, surface the gap clearly.
                    future.set_exception(NotImplementedError(
                        f"StLinkMemAp doesn't yet handle "
                        f"{type(op).__name__}"))
                else:
                    future.set_exception(TypeError(
                        f"StLinkMemAp can't lower {type(op).__name__}"))
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
