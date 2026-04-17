import asyncio

from ...protocol.jtag import Tap, Dr, Instruction
from ..fpga import JtagSramFpga
from ...bitstring import BitString
from .sdm_jtag import SdmJtagTransport


class SdmError(Exception):
    """Raised when SDM communication fails."""


class SdmMailbox:
    """SDM mailbox protocol over an Avalon-ST transport.

    Provides the SDM command layer: sync, simple commands, status
    queries, and multi-word transactions.  The transport is pluggable
    (currently JTAG VIR/VDR via SdmJtagTransport).
    """

    # Busy bit in VDR response payload — always masked out
    BUSY_MASK = 0xFF7FFFFF

    def __init__(self, transport):
        self._tr = transport

    # ------------------------------------------------------------------
    # Low-level transaction helpers
    # ------------------------------------------------------------------

    async def _vdr_read_check(self, n_words, mask_words=None, retries=75):
        """Read VDR responses, optionally check against mask.

        The host always shifts zeros into VDR.  The SDM loads response
        data into the shift register.  mask_words are expected values
        for comparison (from STAPL J80/J81), NOT data to send.

        Reads extra words per attempt and uses a sliding window to
        find the matching response (handles phase misalignment).
        Retries until responses match the mask.
        """
        read_count = n_words + 2  # extra words for alignment

        for attempt in range(retries):
            responses = await self._tr.vdr_read(read_count)

            if mask_words is None:
                return responses[:n_words]

            # Sliding window: try each offset
            for offset in range(len(responses) - n_words + 1):
                window = responses[offset:offset + n_words]
                ok = True
                for i, (payload, valid, last) in enumerate(window):
                    if i < len(mask_words) and mask_words[i] is not None:
                        exp_payload, exp_mask = mask_words[i]
                        if (payload & exp_mask) != (exp_payload & exp_mask):
                            ok = False
                            break
                if ok:
                    return window

        raise SdmError(f"SDM VDR read failed after {retries} retries")

    async def transact(self, vir_words, vdr_expected, vdr_n_response,
                       vdr_mask=None, retries=75, atomic_vir=False):
        """Complete SDM transaction: VIR command + VDR response read.

        vdr_expected is unused — the VDR always receives zeros.
        vdr_mask contains expected values for response checking.
        atomic_vir: if True, send VIR as single DR scan (required
        for SPI commands). Default False for sync/config commands.
        """
        await self._tr.vir_write(vir_words, atomic=atomic_vir)
        return await self._vdr_read_check(
            vdr_n_response, vdr_mask, retries)

    # ------------------------------------------------------------------
    # SDM commands
    # ------------------------------------------------------------------

    async def sync(self, sync2_vir=None, sync2_vdr=None, sync2_mask=None):
        """Synchronize with the SDM.

        Phase 1: flush/reset.
        Phase 2 (optional): device-specific handshake.
        """
        p = self._tr.pack_word

        # Phase 1: flush (query words with no FIRST/LAST = read-only reset)
        flush_vir = [p(0xC0000000), p(0x80000000)]
        ack_vdr = [p(0x00000000, first=True, last=True)]
        ack_mask = [(0x00000000, self.BUSY_MASK)]
        await self.transact(flush_vir, ack_vdr, 1, ack_mask)

        # Phase 2: handshake
        if sync2_vir is not None:
            await self.transact(
                sync2_vir, sync2_vdr,
                len(sync2_vdr), sync2_mask)

    async def command(self, cmd_id, first=True, last=False, retries=75):
        """Send a simple SDM command (bit[31]=1, cmd_id in [7:0]).

        first=True for write/request, False for read/query.
        last=True to close/release.

        Returns single VDR ack response payload.
        """
        p = self._tr.pack_word
        payload = 0x80000000 | (cmd_id & 0xFF)
        vir = [p(payload, first=first, last=last)]
        ack = [p(0, first=True, last=True)]
        ack_mask = [(0x00000000, self.BUSY_MASK)]
        resp = await self.transact(vir, ack, 1, ack_mask, retries)
        return resp[0][0]

    async def status(self):
        """Query SDM status (5 VDR response words).

        Uses cmd_id=0x01 with first=False (read/query mode).
        Returns list of (payload, valid, last) tuples.
        """
        p = self._tr.pack_word
        # Same payload as config request, but first=False = query mode
        vir = [p(0x80000001, first=False)]
        await self._tr.vir_write(vir)
        return await self._tr.vdr_read(5)


class Agilex5(Tap, JtagSramFpga):
    """Altera/Intel Agilex 5 FPGA with SDM.

    All configuration goes through the SDM mailbox over Virtual JTAG.
    See NOTES.md for full protocol documentation.
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
    VIR = Instruction(0x201, None)
    VDR = Instruction(0x202, None)
    CONFIG_STATUS_VJ = Instruction(0x208, None)

    CONF_DONE_BIT = 13

    # Bitstream streaming constants
    _STREAM_HEADER = 0xA17E2A00_FFFFFFFF
    _STREAM_HEADER_BITS = 64
    _STREAM_INITIAL_CHUNK = 32768
    _STREAM_MAX_CHUNK = 524288     # J120 from STAPL
    _STREAM_STATUS_RETRIES = 6000

    # Subclass must set these for sync phase 2
    _SYNC2_VIR = None
    _SYNC2_VDR = None
    _SYNC2_MASK = None

    def _build_sdm(self):
        """Build the SDM mailbox + transport stack."""
        transport = SdmJtagTransport(self, vir_ir=int(self.VIR),
                                     vdr_ir=int(self.VDR))
        return SdmMailbox(transport), transport

    async def child_spawn(self, name):
        if name == "spi":
            return await self._spawn_spi()
        return await super().child_spawn(name)

    async def _spawn_spi(self):
        """Build SPI interface over SDM SPI passthrough.

        Returns spi.Interface with a cs0 Target child.
        Use: tap.child_summon("spi", "cs0") to get the SPI target.
        """
        from ...protocol import spi
        from .sdm_spi import SdmSpiAdapter

        sdm, transport = self._build_sdm()
        p = transport.pack_word

        # Sync with SDM in FLASH mode (different sync2 from config mode!)
        # Sync uses word-by-word VIR (non-atomic)
        await sdm.sync(
            self._FLASH_SYNC2_VIR, self._FLASH_SYNC2_VDR,
            self._FLASH_SYNC2_MASK)

        # Access check — use atomic VIR (SPI bridge commands need it)
        await transport.vir_write([0x200000032], atomic=True)
        await transport.vdr_read(2)

        # Set chip select to CS0 (type 0x34 dynamic command)
        await transport.vir_write([0x100001034, 0x200000000], atomic=True)
        await transport.vdr_read(2)

        adapter = SdmSpiAdapter(sdm, transport)
        interface = spi.Interface(adapter, name="spi")
        target = spi.Target(interface, cs=0, mode=0, name="cs0")
        interface.child_add(target)
        return interface

    # ------------------------------------------------------------------
    # Bitstream streaming (CONFIG DR path, not VIR/VDR)
    # ------------------------------------------------------------------

    async def _stream_status_check(self, request_data=False,
                                   start_config=False, enable=False):
        """Check config status via IR 0x208 (CONFIG_STATUS_VJ).

        Returns (done, error, progress_bits, fifo_free).
        """
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
        """Stream bitstream data to SDM via IR 0x002 (CONFIG).

        Implements the STAPL j127/j125 flow control protocol:
        1. Check status (j125)
        2. If DONE=1 (SDM processing/stalled): loop back to 1,
           do NOT send data.  Halve chunk size.
        3. If DONE=0 (SDM ready): send one chunk, loop to 1.
           Double chunk size on success.
        4. If progress == total: done.
        5. If ERROR: abort.

        The first status check sets request_data + start_config + enable
        to initiate the transfer.  Subsequent checks only set request_data
        when the SDM stalls (DONE=1 with FIFO empty).
        """
        chunk_size = self._STREAM_INITIAL_CHUNK
        consumed = 0    # bits consumed by SDM (from progress counter)
        sent_bits = 0   # bits we've shifted into the DR
        first = True
        stalled = False
        prev_done = False
        prev_fifo_free = 0
        status_retries = self._STREAM_STATUS_RETRIES

        header_bs = BitString(
            self._STREAM_HEADER.to_bytes(8, 'little'),
            self._STREAM_HEADER_BITS)

        while consumed < total_bits:
            # --- Status check (j125) ---
            # STAPL j127 sets request_data (J108) when:
            #   first iteration (J123==1), or
            #   SDM stalled with empty FIFO (J113==0 and J111==1)
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

            # --- Flow control: if DONE, SDM is busy, don't send ---
            if done:
                stalled = True
                # Halve chunk size (but not below initial)
                if chunk_size > self._STREAM_INITIAL_CHUNK:
                    chunk_size //= 2
                continue  # ← the critical GOTO J131 from STAPL

            # --- DONE=0: SDM ready for more data ---
            if stalled:
                stalled = False
            else:
                # SDM keeping up: double chunk size
                chunk_size = min(chunk_size * 2, self._STREAM_MAX_CHUNK)

            # Compute chunk: from consumed position, not sent position.
            # Use the progress counter to know where the SDM is.
            remaining = total_bits - sent_bits
            if remaining <= 0:
                continue

            n = min(chunk_size, remaining)

            # Build DR frame: [64-bit header] [data] [1-bit trailer]
            data_slice = BitString(
                bytes(data[sent_bits // 8:(sent_bits + n + 7) // 8]), n)
            trailer = BitString(0, 1)
            frame = header_bs + data_slice + trailer

            await self.ir(int(self.CONFIG), dr_length=len(frame))(
                frame, read_tdo=False)
            await self.run(16)

            sent_bits += n
            # Reset status retries after successful send
            status_retries = self._STREAM_STATUS_RETRIES

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
        sdm, _transport = self._build_sdm()

        self.logger.trace("Synchronizing with SDM...")
        await sdm.sync(self._SYNC2_VIR, self._SYNC2_VDR, self._SYNC2_MASK)

        self.logger.trace("Requesting configuration...")
        await sdm.command(0x01, first=True, retries=300)

        self.logger.trace("Streaming %d bits...", total_bits)
        with self.progress("config", len(blob), unit="B"):
            await self._stream_bitstream(blob, total_bits)

        self.logger.trace("Waiting for CONF_DONE...")
        for attempt in range(15):
            await asyncio.sleep(0.1)
            status = await sdm.status()
            self.logger.protocol("SDM status: %s",
                                 [(hex(p), s, e) for p, s, e in status[:3]])
            # TODO: proper CONF_DONE extraction from status response
            # For now, check word[0] patterns from observed behavior
            if len(status) >= 1 and status[0][0] == 0x10000000:
                self.logger.note("Configuration complete")
                return

        raise SdmError("Configuration timeout: CONF_DONE not asserted")

    async def erase(self):
        pass

    async def is_configured(self) -> bool:
        status = await self.CHECK_STATUS()
        return bool(int(status) & (1 << self.CONF_DONE_BIT))


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

        p = SdmJtagTransport.pack_word

        # Sync2 for CONFIGURATION mode (from STAPL dj161)
        self._SYNC2_VIR = [
            p(0x7C000400, first=True),
            p(0x8F812007, last=True),
        ]
        self._SYNC2_VDR = [
            p(0xF0001000, first=True),
            p(0x3E04801E, first=True, last=True),
        ]
        self._SYNC2_MASK = [
            (0xF0001000, 0xFF7FFFFF),
            (0x3E04801E, 0xFFFFFFFF),
        ]

        # Sync2 for FLASH ACCESS mode (from STAPL dj0)
        self._FLASH_SYNC2_VIR = [
            p(0x7C000400, first=True),
            p(0x9FCE258F, last=True),         # different from config!
        ]
        self._FLASH_SYNC2_VDR = [
            p(0xF0001000, first=True),
            p(0x7F38963E, first=True, last=True),  # different from config!
        ]
        self._FLASH_SYNC2_MASK = [
            (0xF0001000, 0xFF7FFFFF),
            (0x7F38963E, 0xFFFFFFFF),
        ]
