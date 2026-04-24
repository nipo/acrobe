import asyncio
import enum
import struct

from ...protocol.jtag import Tap, Dr, Instruction
from ..fpga import JtagSramFpga
from ...bitstring import BitString
from ...bitfield import *
from ...component import Component
from .sdm import Sdm, SdmError
from .sdm_jtag import SdmJtag


class Agilex5SdmCommand(enum.IntEnum):
    """Known SDM command opcodes for Agilex 5."""
    NOOP = 0x000
    SYNC = 0x001
    CONFIG_STATUS = 0x004
    CONFIG_REQUEST = 0x005

    GET_IDCODE = 0x010
    GET_CHIPID = 0x012
    GET_USERCODE = 0x013
    GET_VOLTAGE = 0x018
    GET_TEMPERATURE = 0x019
    GET_CONFIGURATION_TIME = 0x065

    READ_SEU_ERROR = 0x03c
    STATUS_VR = 0x713

    QSPI_OPEN = 0x032
    QSPI_CLOSE = 0x033
    QSPI_SET_CS = 0x034
    QSPI_READ_DEVICE_REG = 0x035
    QSPI_WRITE_DEVICE_REG = 0x036
    QSPI_SEND_DEVICE_OP = 0x037
    QSPI_READ_SHA = 0x06e
    QSPI_ERASE = 0x038
    QSPI_WRITE = 0x039
    QSPI_READ = 0x03A

    RSU_GET_SPT = 0x05A
    RSU_STATUS = 0x05B
    RSU_IMAGE_UPDATE = 0x5c
    RSU_NOTIFY = 0x5d


class Agilex5(Tap, JtagSramFpga):
    """Altera/Intel Agilex 5 FPGA with SDM.

    All configuration goes through the SDM over JTAG.
    See NOTES_SDM_REVISED.md for protocol documentation.
    """

    irlen = 10
    max_freq = 12e6

    # DR descriptors
    DEVICE_ID = Dr(32)
    BYPASS_REG = Dr(1)
    USER_CODE = Dr(32)
    CONFIG_STATUS_DR = Dr(492)

    # Core JTAG instructions
    CONFIG = Instruction(0x002, None)
    CHECK_STATUS = Instruction(0x004, "CONFIG_STATUS_DR")
    IDCODE = Instruction(0x006, "DEVICE_ID")
    USERCODE = Instruction(0x007, "USER_CODE")
    BYPASS = Instruction(0x3FF, "BYPASS_REG")

    # SDM JTAG instructions
    SDM_IO = Dr(34)
    SDM_CMD = Instruction(0x201, "SDM_IO")
    SDM_RSP = Instruction(0x202, "SDM_IO")
    SDM_WAKEUP = Instruction(0x281, None)

    CONFIG_STATUS_REG = Dr(40)
    CONFIG_STATUS_VJ = Instruction(0x208, "CONFIG_STATUS_REG")

    CONF_DONE_BIT = 13

    # Bitstream streaming constants
    _STREAM_HEADER = 0xA17E2A00_FFFFFFFF
    _STREAM_HEADER_BITS = 64
    _STREAM_INITIAL_CHUNK = 32768
    _STREAM_MAX_CHUNK = 524288
    _STREAM_STATUS_RETRIES = 6000

    def _build_sdm(self) -> Sdm:
        """Build the SDM transport stack."""
        return SdmJtag(self)

    async def child_spawn(self, name):
        if name == "sdm":
            return await self._spawn_sdm_client()
        if name == "spi":
            return await self._spawn_spi()
        return await super().child_spawn(name)

    async def _spawn_sdm_client(self):
        """Spawn an SDM client component for interactive use."""
        sdm = self._build_sdm()
        await sdm.sync()
        client = AgilexSdmClient(sdm)
        return client

    async def _spawn_spi(self):
        """Build SPI interface over SDM SPI passthrough."""
        from ...protocol import spi
        from .sdm_spi import SdmSpiAdapter

        sdm = self._build_sdm()
        await sdm.sync()

        # QSPI_OPEN
        await sdm.command(Agilex5SdmCommand.QSPI_OPEN)

        # Set CS0
        await sdm.command(Agilex5SdmCommand.QSPI_SET_CS, b'\x00\x00\x00\x00')

        adapter = SdmSpiAdapter(sdm)
        interface = spi.Interface(adapter, name="spi")
        target = spi.Target(interface, cs=0, mode=0, name="cs0")
        interface.child_add(target)
        return interface

    # ------------------------------------------------------------------
    # SramFpga interface
    # ------------------------------------------------------------------

    async def start(self):
        configured = await self.is_configured()
        self.logger.note("IDCODE: 0x%08x, configured: %s",
                         self.idcode, configured)

    async def load(self, program):
        """Load bitstream into Agilex 5 via SDM."""
        if len(program) != 1:
            raise ValueError("Bitstream requires exactly one segment")

        blob = program[0].data
        total_bits = len(blob) * 8
        sdm = self._build_sdm()

        self.logger.trace("Synchronizing with SDM...")
        await sdm.sync()

        self.logger.trace("Requesting configuration...")
        await sdm.command(Agilex5SdmCommand.CONFIG_REQUEST)

        self.logger.trace("Streaming %d bits...", total_bits)
        with self.progress("config", len(blob), unit="B"):
            await self._stream_bitstream(blob, total_bits)

        self.logger.trace("Waiting for CONF_DONE...")
        for attempt in range(15):
            await asyncio.sleep(0.1)
            try:
                status = await sdm.command(Agilex5SdmCommand.CONFIG_STATUS)
                self.logger.protocol("SDM status: %s",
                                     status.hex(' '))
            except SdmError:
                continue

        raise SdmError(0, opcode=int(Agilex5SdmCommand.CONFIG_STATUS))

    async def erase(self):
        pass

    async def is_configured(self) -> bool:
        status = await self.CHECK_STATUS()
        return bool(int(status) & (1 << self.CONF_DONE_BIT))

    # ------------------------------------------------------------------
    # Bitstream streaming (CONFIG DR path)
    # ------------------------------------------------------------------

    async def _stream_status_check(self, request_data=False,
                                   start_config=False, enable=False):
        """Check config status via IR 0x208."""
        dr_bits = 37
        tdi_val = 0
        if request_data:
            tdi_val |= 1
        if start_config:
            tdi_val |= 2
        if enable:
            tdi_val |= 4

        tdi = BitString(tdi_val, dr_bits)
        result = await self.ir(int(self.CONFIG_STATUS_VJ), dr_length=dr_bits)(
            tdi, read_tdo=True)
        await self.run(16)

        val = int(result)
        done = bool(val & 1)
        error = bool(val & 2)
        progress_words = (val >> 2) & 0x3FFFFFFF
        fifo_free = (val >> 32) & 0x1F
        return done, error, progress_words * 32, fifo_free

    async def _stream_bitstream(self, data, total_bits):
        """Stream bitstream data to SDM via IR 0x002 (CONFIG)."""
        chunk_size = self._STREAM_INITIAL_CHUNK
        consumed = 0
        sent_bits = 0
        first = True
        stalled = False
        prev_done = False
        prev_fifo_free = 0
        status_retries = self._STREAM_STATUS_RETRIES

        header_bs = BitString(
            self._STREAM_HEADER.to_bytes(8, 'little'),
            self._STREAM_HEADER_BITS)

        while consumed < total_bits:
            request_data = first or (prev_done and prev_fifo_free == 0)
            done, error, progress, fifo_free = await self._stream_status_check(
                request_data=request_data,
                start_config=first,
                enable=first,
            )
            prev_done = done
            prev_fifo_free = fifo_free

            if first:
                first = False

            if error:
                raise SdmError(
                    0,
                    opcode=int(Agilex5SdmCommand.CONFIG_REQUEST))

            consumed = progress
            if consumed >= total_bits:
                break

            status_retries -= 1
            if status_retries <= 0:
                raise SdmError(
                    0,
                    opcode=int(Agilex5SdmCommand.CONFIG_REQUEST))

            if done:
                stalled = True
                if chunk_size > self._STREAM_INITIAL_CHUNK:
                    chunk_size //= 2
                continue

            if stalled:
                stalled = False
            else:
                chunk_size = min(chunk_size * 2, self._STREAM_MAX_CHUNK)

            remaining = total_bits - sent_bits
            if remaining <= 0:
                continue

            n = min(chunk_size, remaining)

            data_slice = BitString(
                bytes(data[sent_bits // 8:(sent_bits + n + 7) // 8]), n)
            trailer = BitString(0, 1)
            frame = header_bs + data_slice + trailer

            await self.ir(int(self.CONFIG), dr_length=len(frame))(
                frame, read_tdo=False)
            await self.run(16)

            sent_bits += n
            status_retries = self._STREAM_STATUS_RETRIES


# ------------------------------------------------------------------
# SDM client component
# ------------------------------------------------------------------

class ConfigStatus(Bitfield):
    # Word 0
    state = BooleanField(0)
    # Word 1
    fw_version = Field(32+28, 4)
    quartus_major = Field(32+16, 8)
    quartus_minor = Field(32+8, 8)
    quartus_update = Field(32, 8)
    # Word 2
    nSTATUS = BooleanField(64+31)
    nCONFIG = BooleanField(64+30)
    clk_src = MappingField(64+6, 2, [None, "Int", "Clk1", "SecurePll"])
    vid_enabled = BooleanField(64+4)
    msel = MappingField(64+0, 3, {
        1: "AS-Fast",
        3: "AS-Normal",
        5: "AvSTx16",
        6: "AvSTx8",
        7: "JTAG",
    })
    # Word 3
    hps_warmreset = BooleanField(96+5)
    hps_coldreset = BooleanField(96+4)
    seu_error = BooleanField(96+3)
    cvp_done = BooleanField(96+2)
    init_done = BooleanField(96+1)
    conf_done = BooleanField(96+0)
    # Word 4
    error_location = Field(128, 32)
    # Word 5
    error_details = Field(160, 32)

class AgilexSdmClient(Component):
    """High-level SDM command interface for Agilex devices.

    Provides typed methods for each known SDM command.
    Use via: await tap.child_summon("sdm")
    """

    def __init__(self, sdm: Sdm):
        super().__init__("agilex_sdm")
        self._sdm = sdm

    async def get_idcode(self) -> int:
        """Read IDCODE via SDM."""
        data = await self._sdm.command(Agilex5SdmCommand.GET_IDCODE)
        return int.from_bytes(data[:4], 'little')

    async def get_chipid(self) -> int:
        """Read 64-bit unique chip ID."""
        data = await self._sdm.command(Agilex5SdmCommand.GET_CHIPID)
        return int.from_bytes(data[:8], 'little')

    async def get_usercode(self) -> int:
        """Read USERCODE."""
        data = await self._sdm.command(Agilex5SdmCommand.GET_USERCODE)
        return int.from_bytes(data[:4], 'little')

    async def config_status(self) -> bytes:
        """Read configuration status (6 words = 24 bytes)."""
        v = await self._sdm.command(Agilex5SdmCommand.CONFIG_STATUS)
        v = int.from_bytes(v, "little")
        return ConfigStatus(all = v)

    async def qspi_open(self):
        """Enable QSPI flash access."""
        await self._sdm.command(Agilex5SdmCommand.QSPI_OPEN)

    async def qspi_close(self):
        """Disable QSPI flash access."""
        await self._sdm.command(Agilex5SdmCommand.QSPI_CLOSE)

    async def qspi_set_cs(self, cs: int):
        """Select QSPI chip select line (0-3)."""
        await self._sdm.command(
            Agilex5SdmCommand.QSPI_SET_CS,
            (cs << 28).to_bytes(4, 'little'))

    async def qspi_read(self, address: int, word_count: int) -> bytes:
        """Read from QSPI flash. Address must be word-aligned."""
        arg = struct.pack('<II', address, word_count)
        return await self._sdm.command(Agilex5SdmCommand.QSPI_READ, arg)

    async def qspi_write(self, address: int, data: bytes):
        """Write to QSPI flash. Address must be word-aligned."""
        word_count = (len(data) + 3) // 4
        arg = struct.pack('<II', address, word_count) + data
        await self._sdm.command(Agilex5SdmCommand.QSPI_WRITE, arg)

    async def qspi_erase(self, address: int, word_count: int):
        """Erase QSPI flash region. Address must be word-aligned."""
        arg = struct.pack('<II', address, word_count)
        await self._sdm.command(Agilex5SdmCommand.QSPI_ERASE, arg)

    async def qspi_read_device_reg(self, opcode: int, byte_count: int) -> bytes:
        """Read a QSPI device register (e.g. JEDEC ID)."""
        arg = struct.pack('<II', opcode, byte_count)
        return await self._sdm.command(
            Agilex5SdmCommand.QSPI_READ_DEVICE_REG, arg)


# ------------------------------------------------------------------
# Concrete parts
# ------------------------------------------------------------------

_AGILEX5_PARTS = {
    0x0364f0dd: "A5ED065BB32AR0",
    0x0362c0dd: "A5EA013BB23B",
}


@Tap.db.register(*_AGILEX5_PARTS.keys())
class Agilex5E(Agilex5):
    """Agilex 5 E-series."""

    def __init__(self, interface, idcode, **kw):
        name = _AGILEX5_PARTS.get(
            idcode & 0xfffffff, f"Agilex5-0x{idcode:08x}")
        super().__init__(interface, idcode, name=name, **kw)
