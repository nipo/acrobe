"""SPI Flash component.

Provides read, erase, and program operations for SPI NOR flash devices.
Uses the SPI protocol layer for communication.
"""

from __future__ import annotations

import asyncio
import struct

from ..component import Component
from ..protocol.spi import Cs, Shift


class SpiFlash(Component):
    """SPI NOR flash device.

    Communicates through an SPI interface (Batcher that accepts Cs/Shift ops).
    """

    # Standard SPI flash commands
    CMD_READ_JEDEC_ID = b"\x9f"
    CMD_FAST_READ = b"\x0b"
    CMD_PAGE_PROGRAM = b"\x02"
    CMD_CHIP_ERASE = b"\xc7"
    CMD_WRITE_ENABLE = b"\x06"
    CMD_WRITE_DISABLE = b"\x04"
    CMD_READ_STATUS = b"\x05"
    CMD_RESET_ENABLE = b"\x66"
    CMD_RESET = b"\x99"

    STATUS_WIP = 0x01  # Write In Progress
    STATUS_WEL = 0x02  # Write Enable Latch

    # Sector erase commands (size -> command byte)
    SECTOR_ERASE_4K = b"\x20"
    BLOCK_ERASE_32K = b"\x52"
    BLOCK_ERASE_64K = b"\xd8"

    ADDRESS_SIZE = 3  # 3-byte addressing by default

    def __init__(self, interface, cs=0, mode: int = 0, name: str = "flash"):
        super().__init__(name)
        self._interface = interface
        self._cs = cs
        self._mode = mode
        self.jedec_id = 0
        self.total_size = 0
        self.page_size = 256
        self.sector_info = []  # list of (size, erase_cmd)

    async def _command(self, cmd: bytes, addr: bytes = b"",
                       wdata: bytes = b"", rsize: int = 0,
                       dummy: int = 0) -> bytes:
        """Execute a flash command: CS assert, send command+addr+data, read response, CS deassert."""
        parts = [cmd]
        if addr:
            parts.append(addr)
        if dummy:
            parts.append(bytes(dummy))
        if wdata:
            parts.append(wdata)

        mosi = b"".join(parts)

        await self._interface.post(Cs(self._cs, self._mode))

        if rsize:
            # Send command, then read response
            await self._interface.post(Shift(mosi, read_miso=False))
            shift = Shift(rsize, read_miso=True)
            await self._interface.post(shift)
            await self._interface.post(Cs(None))
            return shift.miso
        else:
            await self._interface.post(Shift(mosi, read_miso=False))
            await self._interface.post(Cs(None))
            return b""

    def _addr_bytes(self, addr: int) -> bytes:
        return addr.to_bytes(self.ADDRESS_SIZE, "big")

    # --- Status ---

    async def read_status(self) -> int:
        """Read the status register."""
        data = await self._command(self.CMD_READ_STATUS, rsize=1)
        return data[0]

    async def _wait_ready(self):
        """Poll until WIP bit clears."""
        while True:
            status = await self.read_status()
            if not (status & self.STATUS_WIP):
                return status
            await asyncio.sleep(0.001)

    # --- Identification ---

    async def read_jedec_id(self) -> int:
        """Read 3-byte JEDEC ID (manufacturer, device type, capacity)."""
        data = await self._command(self.CMD_READ_JEDEC_ID, rsize=3)
        return int.from_bytes(data, "big")

    async def detect(self):
        """Reset flash, read JEDEC ID, and configure geometry."""
        # Software reset
        await self._command(self.CMD_RESET_ENABLE)
        await self._command(self.CMD_RESET)
        await asyncio.sleep(0.01)

        self.jedec_id = await self.read_jedec_id()
        if self.jedec_id in (0, 0xffffff):
            raise RuntimeError(f"Bad JEDEC ID: 0x{self.jedec_id:06x}")

        self.logger.info("JEDEC ID: 0x%06x", self.jedec_id)

        # Derive size from capacity byte (common convention: 2^capacity)
        capacity_byte = self.jedec_id & 0xff
        if capacity_byte >= 0x10:
            self.total_size = 1 << capacity_byte

        # Default sector info if not set
        if not self.sector_info:
            self.sector_info = [
                (4096, self.SECTOR_ERASE_4K),
                (65536, self.BLOCK_ERASE_64K),
            ]

    # --- Read ---

    async def read(self, addr: int, size: int) -> bytes:
        """Read data from flash using fast read command."""
        result = bytearray()
        while size > 0:
            chunk = min(size, 1024)
            data = await self._command(
                self.CMD_FAST_READ,
                addr=self._addr_bytes(addr),
                rsize=chunk,
                dummy=1)
            result.extend(data)
            addr += chunk
            size -= chunk
        return bytes(result)

    # --- Write enable/disable ---

    async def write_enable(self):
        """Set the Write Enable Latch."""
        await self._command(self.CMD_WRITE_ENABLE)

    async def write_disable(self):
        """Clear the Write Enable Latch."""
        await self._command(self.CMD_WRITE_DISABLE)

    # --- Erase ---

    async def erase_sector(self, addr: int, erase_cmd: bytes = None):
        """Erase a sector at the given address."""
        if erase_cmd is None:
            erase_cmd = self.SECTOR_ERASE_4K
        await self.write_enable()
        await self._command(erase_cmd, addr=self._addr_bytes(addr))
        await self._wait_ready()

    async def erase_chip(self):
        """Erase the entire chip."""
        await self.write_enable()
        await self._command(self.CMD_CHIP_ERASE)
        await self._wait_ready()

    async def erase(self, addr: int, size: int):
        """Erase a region, choosing appropriate sector sizes."""
        while size > 0:
            # Find largest erase that fits
            chosen_size = None
            chosen_cmd = None
            for s_size, s_cmd in sorted(self.sector_info, reverse=True):
                if addr % s_size == 0 and size >= s_size:
                    chosen_size = s_size
                    chosen_cmd = s_cmd
                    break

            if chosen_size is None:
                # Fall back to smallest
                chosen_size, chosen_cmd = self.sector_info[0]
                assert addr % chosen_size == 0

            await self.erase_sector(addr, chosen_cmd)
            addr += chosen_size
            size -= chosen_size

    # --- Program ---

    async def page_program(self, addr: int, data: bytes):
        """Program a page (up to page_size bytes)."""
        assert len(data) <= self.page_size
        await self.write_enable()
        await self._command(self.CMD_PAGE_PROGRAM,
                            addr=self._addr_bytes(addr),
                            wdata=data)
        await self._wait_ready()

    async def program(self, addr: int, data: bytes):
        """Program data, splitting into page-aligned chunks."""
        offset = 0
        while offset < len(data):
            # Align to page boundary
            page_offset = addr % self.page_size
            chunk_size = min(self.page_size - page_offset, len(data) - offset)
            chunk = data[offset:offset + chunk_size]
            await self.page_program(addr, chunk)
            addr += chunk_size
            offset += chunk_size

    # --- Verify ---

    async def verify(self, addr: int, data: bytes) -> bool:
        """Verify flash contents match expected data."""
        readback = await self.read(addr, len(data))
        return readback == data

    def __repr__(self):
        return f"<SpiFlash jedec={self.jedec_id:#08x} size={self.total_size}>"
