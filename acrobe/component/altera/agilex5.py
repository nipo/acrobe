import asyncio

from ...protocol.jtag import Tap, Dr, Instruction
from ..fpga import JtagSramFpga
from ...bitstring import BitString
from .sdm_jtag import SdmJtagTransport, SdmError


class Agilex5(Tap, JtagSramFpga):
    """Altera/Intel Agilex 5 FPGA with SDM.

    All configuration goes through the SDM mailbox over JTAG.
    See NOTES_SDM_REVISED.md for full protocol documentation.
    """

    irlen = 10
    max_freq = 12e6

    # DR descriptors
    DEVICE_ID = Dr(32)
    BYPASS_REG = Dr(1)
    USER_CODE = Dr(32)
    CONFIG_STATUS = Dr(492)

    # Core JTAG instructions
    CONFIG = Instruction(0x002, None)
    CHECK_STATUS = Instruction(0x004, "CONFIG_STATUS")
    IDCODE = Instruction(0x006, "DEVICE_ID")
    USERCODE = Instruction(0x007, "USER_CODE")
    BYPASS = Instruction(0x3FF, "BYPASS_REG")

    # SDM Virtual JTAG instructions
    SDM_CMD = Instruction(0x201, None)
    SDM_RSP = Instruction(0x202, None)
    CONFIG_STATUS_VJ = Instruction(0x208, None)

    CONF_DONE_BIT = 13

    # Bitstream streaming constants
    _STREAM_HEADER = 0xA17E2A00_FFFFFFFF
    _STREAM_HEADER_BITS = 64
    _STREAM_INITIAL_CHUNK = 32768
    _STREAM_MAX_CHUNK = 524288     # J120 from STAPL
    _STREAM_STATUS_RETRIES = 6000

    # Subclass must set sync nonce
    _SYNC_NONCE = None
    _SYNC_UPPER = 0xF

    def _build_sdm(self):
        """Build the SDM transport stack."""
        return SdmJtagTransport(self._interface)

    async def child_spawn(self, name):
        if name == "spi":
            return await self._spawn_spi()
        return await super().child_spawn(name)

    async def _spawn_spi(self):
        """Build SPI interface over SDM SPI passthrough."""
        from ...protocol import spi
        from .sdm_spi import SdmSpiAdapter

        sdm = self._build_sdm()

        # Sync
        await sdm.sync(self._SYNC_NONCE, self._SYNC_UPPER)

        # QSPI_OPEN
        error, _ = await sdm.command(0x32)
        if error:
            raise SdmError(f"QSPI_OPEN failed: error {error}")

        # Set CS0
        error, _ = await sdm.command(0x34, args=[0])
        if error:
            raise SdmError(f"QSPI_SET_CS failed: error {error}")

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
        await sdm.sync(self._SYNC_NONCE, self._SYNC_UPPER)

        self.logger.trace("Requesting configuration...")
        # Config request: opcode 5, no args
        error, _ = await sdm.command(0x05)
        if error:
            raise SdmError(f"Config request failed: error {error}")

        self.logger.trace("Streaming %d bits...", total_bits)
        with self.progress("config", len(blob), unit="B"):
            await self._stream_bitstream(blob, total_bits)

        self.logger.trace("Waiting for CONF_DONE...")
        for attempt in range(15):
            await asyncio.sleep(0.1)
            error, status = await sdm.command(0x04, max_response=6)
            if error:
                self.logger.protocol("SDM status error: %d", error)
                continue
            self.logger.protocol("SDM status: %s",
                                 [f'{w:#010x}' for w in status])
            # TODO: proper CONF_DONE extraction from status response

        raise SdmError("Configuration timeout: CONF_DONE not asserted")

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
        """Check config status via IR 0x208 (CONFIG_STATUS_VJ)."""
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
                    f"SDM error during streaming "
                    f"(consumed={consumed}/{total_bits})")

            consumed = progress

            if consumed >= total_bits:
                break

            status_retries -= 1
            if status_retries <= 0:
                raise SdmError(
                    f"SDM streaming timeout "
                    f"(consumed={consumed}/{total_bits})")

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
        name = _AGILEX5_PARTS.get(idcode & 0xfffffff, f"Agilex5-0x{idcode:08x}")
        super().__init__(interface, idcode, name=name, **kw)

        # Device-specific sync nonce (from STAPL)
        # Each Agilex 5 part has a unique nonce for the SYNC handshake
        part = idcode & 0xfffffff
        if part == 0x0362c0dd:
            self._SYNC_NONCE = 0xAB92C300
        elif part == 0x0364f0dd:
            self._SYNC_NONCE = 0x7F38963E
        else:
            self._SYNC_NONCE = None
