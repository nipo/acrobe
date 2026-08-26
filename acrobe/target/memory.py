"""Memory view on a Target — a Node grouping addressable Region
children backed by a real memory bus (Mem-AP on Cortex-M, system-
bus access on a RISC-V Debug Module, instruction-stuffing on
ARM9, etc.).

The Memory view is the natural anchor for clients that need
bounded, memory-mapped access:

* SEGGER RTT spawned under a SRAM child — control block scan is
  bounded by the parent Ram's address/size.
* (Future) Peripheral drivers under APB / AHB pseudo-Ram regions
  — e.g. a SPI controller exposes itself as `spi.Interface`.
* (Future) Live structure parsers — a Cortex-M VTOR-bank reader
  under PPB.

Memory itself is a thin container — its job is to hold the bus
ref and to keep the address-bounded children together. The
client classes register against `Ram.db` (see `region.py`).
"""

from __future__ import annotations

from ..node import Node


class Memory(Node):
    """Memory-access view on a Target. Holds a bus reference plus
    addressable Region children (typically `BusRam` / `BusFlash`)
    backed by that bus."""

    def __init__(self, bus, name: str = "memory"):
        super().__init__(name)
        self.bus = bus

    async def read(self, addr: int, size: int) -> bytes:
        """Convenience pass-through to the bus."""
        return await self.bus.mem_read(addr, size)

    async def write(self, addr: int, data: bytes) -> None:
        await self.bus.mem_write(addr, data)
