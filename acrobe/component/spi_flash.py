"""SPI Flash component.

Provides read, erase, and program operations for SPI NOR flash devices.
Uses the SPI protocol layer for communication.

The chip's contents are an address space in the
:mod:`acrobe.protocol.memory` sense, so reads and programs are posted
as ``ReadBlob`` / ``WriteBlob`` ops and lowered to flash commands in
one place. Only the bulk family exists here: a NOR flash has no
register window, and emulating one would mean issuing a whole
command frame to fetch four bytes while pretending it was a register.
"""

from __future__ import annotations

import asyncio
import struct

from ..engine import Batcher
from ..node import Node, Readable, Writable, Addressable
from ..protocol import memory
from ..protocol.spi import Shift
from ..util.pretty import base2

# Common JEDEC manufacturer IDs (bank 0)
_JEDEC_MANUFACTURERS = {
    0x01: "AMD/Spansion",
    0x1f: "Atmel",
    0x20: "Micron/Numonyx",
    0x9d: "ISSI",
    0xbf: "Microchip/SST",
    0xc2: "Macronix",
    0xc8: "GigaDevice",
    0xef: "Winbond",
    0x68: "Boya",
    0x0b: "XTX",
    0x5e: "Zbit",
    0x85: "Puya",
    0x25: "Zetta",
}


class SpiFlash(memory.Interface, memory.BackgroundLowering, Batcher,
               Node, Writable, Addressable):  # Writable extends Readable
    """SPI NOR flash device.

    Communicates through an SPI Target (handles CS management).

    Implements Readable + Writable + Addressable so that VFS
    walks can compose file containers on top of live flash
    contents (e.g. flash/as(type=pof)/partition/0/as(type=altera_rbf)).
    `load_address` is 0 (flash address space starts at 0); `size`
    is `total_size` (set by detect()).

    Writable.write() programs data at the given offset; the caller
    is responsible for erasing the affected sectors first
    (Writable's contract is in-place passthrough).
    """

    ops = memory.Interface.BULK_OPS

    # Bytes fetched per FAST_READ command. The SPI transaction is
    # held in one CS assertion, so this only bounds the size of a
    # single adapter transfer.
    READ_CHUNK = 1024

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

    def __init__(self, target, name: str = "flash"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__target = target
        self.jedec_id = 0
        self.total_size = 0
        self.page_size = 256
        self.sector_info = []  # list of (size, erase_cmd)

    # --- Readable / Addressable contract ---

    @property
    def size(self) -> int:
        return self.total_size

    @property
    def load_address(self) -> int:
        return 0

    # --- Writable contract ---

    async def write(self, offset: int, data: bytes) -> None:
        """Writable interface: program `data` at `offset`.

        The caller is responsible for ensuring the relevant
        sectors were erased; Writable's in-place semantics do not
        imply erase-and-write.
        """
        if offset < 0 or offset + len(data) > self.total_size:
            raise ValueError(
                f"write [{offset}, {offset + len(data)}) out of "
                f"flash range [0, {self.total_size})")
        await self.mem_write(offset, data)

    # --- Address-space lowering ---

    async def flush_ops(self, batch):
        self.dispatch(batch)

    async def run_ops(self, batch):
        for op, future in batch:
            try:
                if isinstance(op, memory.ReadBlob):
                    result = await self.__read_chunks(op.addr, op.size)
                elif isinstance(op, memory.WriteBlob):
                    await self.__program_pages(op.addr, op.data)
                    result = None
                else:
                    raise TypeError(
                        f"SpiFlash can't lower {type(op).__name__}")
            except Exception as exc:
                if future is not None:
                    future.set_exception(exc)
                continue
            if future is not None:
                future.set_result(result)

    async def __read_chunks(self, addr: int, size: int) -> bytes:
        result = bytearray()
        while size > 0:
            chunk = min(size, self.READ_CHUNK)
            result += await self.__command(
                self.CMD_FAST_READ,
                addr=self.__addr_bytes(addr),
                rsize=chunk,
                dummy=1)
            addr += chunk
            size -= chunk
        return bytes(result)

    async def __program_pages(self, addr: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            page_offset = addr % self.page_size
            chunk_size = min(self.page_size - page_offset,
                             len(data) - offset)
            await self.page_program(addr, data[offset:offset + chunk_size])
            addr += chunk_size
            offset += chunk_size

    async def __command(self, cmd: bytes, addr: bytes = b"",
                       wdata: bytes = b"", rsize: int = 0,
                       dummy: int = 0) -> bytes:
        """Execute a flash command as an atomic CS-held transaction."""
        parts = [cmd]
        if addr:
            parts.append(addr)
        if dummy:
            parts.append(bytes(dummy))
        if wdata:
            parts.append(wdata)
        mosi = b"".join(parts)

        self.logger.protocol("<< %s %s %s %d", cmd.hex(), addr.hex(), wdata[:16].hex() if wdata else "", rsize)

        shifts = [Shift(mosi, read_miso=False)]
        read_shift = None
        if rsize:
            read_shift = Shift(rsize, read_miso=True)
            shifts.append(read_shift)
        await self.__target.transaction(*shifts)

        rsp = read_shift.miso if read_shift else b""
        if rsp:
            self.logger.protocol(">> %s", rsp[:32].hex())
        return rsp

    def __addr_bytes(self, addr: int) -> bytes:
        return addr.to_bytes(self.ADDRESS_SIZE, "big")

    # --- Status ---

    async def read_status(self) -> int:
        """Read the status register."""
        data = await self.__command(self.CMD_READ_STATUS, rsize=1)
        return data[0]

    async def __wait_ready(self):
        """Poll until WIP bit clears."""
        while True:
            status = await self.read_status()
            if not (status & self.STATUS_WIP):
                return status
            await asyncio.sleep(0.001)

    # --- Identification ---

    async def read_jedec_id(self) -> int:
        """Read 3-byte JEDEC ID (manufacturer, device type, capacity)."""
        data = await self.__command(self.CMD_READ_JEDEC_ID, rsize=3)
        return int.from_bytes(data, "big")

    CMD_SFDP_READ = b"\x5a"

    async def detect(self):
        """Reset flash, read JEDEC ID, configure geometry from SFDP or defaults."""
        # Software reset
        await self.__command(self.CMD_RESET_ENABLE)
        await self.__command(self.CMD_RESET)
        await asyncio.sleep(0.01)

        self.jedec_id = await self.read_jedec_id()
        if self.jedec_id in (0, 0xffffff):
            raise RuntimeError(f"Bad JEDEC ID: 0x{self.jedec_id:06x}")

        mfr = (self.jedec_id >> 16) & 0xff
        mfr_name = _JEDEC_MANUFACTURERS.get(mfr, f"0x{mfr:02x}")
        self.logger.note("JEDEC ID: 0x%06x (%s)", self.jedec_id, mfr_name)

        # Derive size from capacity byte (common convention: 2^capacity)
        capacity_byte = self.jedec_id & 0xff
        if capacity_byte >= 0x10:
            self.total_size = 1 << capacity_byte

        # Try SFDP for detailed parameters
        try:
            await self.__sfdp_detect()
        except Exception:
            # SFDP not supported or parse failed, use defaults
            if not self.sector_info:
                self.sector_info = [
                    (4096, self.SECTOR_ERASE_4K),
                    (65536, self.BLOCK_ERASE_64K),
                ]

        self.logger.note("Size: %s, page: %s, addr: %dB",
                         base2(self.total_size, "B"),
                         base2(self.page_size, "B"),
                         self.ADDRESS_SIZE)
        for size, cmd in self.sector_info:
            self.logger.info("Erase: %s (cmd 0x%02x)", base2(size, "B"), cmd[0])

    async def __sfdp_read(self, addr, size):
        """Read SFDP data at the given address."""
        addr_bytes = addr.to_bytes(3, "big")
        return await self.__command(self.CMD_SFDP_READ, addr=addr_bytes,
                                   rsize=size, dummy=1)

    async def __sfdp_detect(self):
        """Parse SFDP header and basic flash parameter table."""
        header = await self.__sfdp_read(0, 8)
        if header[:4] != b'SFDP':
            return

        sfdp_minor, sfdp_major = header[4], header[5]
        nph = header[6] + 1
        self.logger.note("SFDP v%d.%d, %d parameter header(s)", sfdp_major, sfdp_minor, nph)

        # Read all parameter headers
        ph_data = await self.__sfdp_read(8, nph * 8)

        # Find JEDEC Basic Flash Parameter (ID 0xff00)
        for i in range(nph):
            ph = ph_data[i * 8:(i + 1) * 8]
            jid_lo, minor, major, length = ph[0], ph[1], ph[2], ph[3]
            ptp = int.from_bytes(ph[4:8], "little")
            jid = jid_lo | ((ptp >> 16) & 0xff00)
            ptp = ptp & 0xffffff

            if jid == 0xff00:
                # Parse JEDEC basic flash parameters
                bfp = await self.__sfdp_read(ptp, length * 4)
                self.__sfdp_parse_basic(bfp)
                break

    def __sfdp_parse_basic(self, data):
        """Parse JEDEC Basic Flash Parameter Table (JESD216)."""
        # Density (bytes 4-7)
        density = struct.unpack_from("<L", data, 4)[0]
        if density & 0x80000000:
            self.total_size = 1 << ((density & 0x7fffffff) - 3)
        else:
            self.total_size = (density + 1) // 8

        # Erase sector types (bytes 28-35, 4 types x 2 bytes)
        self.sector_info = []
        for i in range(4):
            off = 28 + 2 * i
            if off + 1 >= len(data):
                break
            log2_size = data[off]
            opcode = data[off + 1]
            if log2_size and opcode:
                self.sector_info.append((1 << log2_size, bytes([opcode])))

        # Page size (byte 0x28, if SFDP 1.6+)
        if len(data) > 0x29:
            page_bits = (data[0x28] >> 4) & 0x0f
            if page_bits:
                self.page_size = 1 << page_bits

    # --- Read ---

    async def read(self, addr: int, size: int) -> bytes:
        """Read data from flash using fast read command."""
        return await self.mem_read(addr, size)

    # --- Write enable/disable ---

    async def write_enable(self):
        """Set the Write Enable Latch."""
        await self.__command(self.CMD_WRITE_ENABLE)

    async def write_disable(self):
        """Clear the Write Enable Latch."""
        await self.__command(self.CMD_WRITE_DISABLE)

    # --- Erase ---

    async def erase_sector(self, addr: int, erase_cmd: bytes = None):
        """Erase a sector at the given address."""
        if erase_cmd is None:
            erase_cmd = self.SECTOR_ERASE_4K
        await self.write_enable()
        await self.__command(erase_cmd, addr=self.__addr_bytes(addr))
        await self.__wait_ready()

    async def erase_chip(self):
        """Erase the entire chip."""
        await self.write_enable()
        await self.__command(self.CMD_CHIP_ERASE)
        await self.__wait_ready()

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
        await self.__command(self.CMD_PAGE_PROGRAM,
                            addr=self.__addr_bytes(addr),
                            wdata=data)
        await self.__wait_ready()

    async def program(self, addr: int, data: bytes):
        """Program data, splitting into page-aligned chunks."""
        await self.mem_write(addr, data)

    # --- Verify ---

    async def verify(self, addr: int, data: bytes) -> bool:
        """Verify flash contents match expected data."""
        readback = await self.read(addr, len(data))
        return readback == data

    def __repr__(self):
        return f"<SpiFlash jedec={self.jedec_id:#08x} size={self.total_size}>"


from ..protocol import spi  # noqa: E402


@spi.Target.child_db.register("flash")
async def _spi_flash_probe(target):
    flash = SpiFlash(target)
    await flash.detect()
    return flash
