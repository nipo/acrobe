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

    async def _vdr_transact(self, send_words, n_response,
                            mask_words=None, retries=75):
        """Send VDR words, collect responses, optionally check mask.

        Because the VDR is a shift register with pipelined responses,
        this collects all TDO values and searches for a matching window.
        """
        for attempt in range(retries):
            responses = await self._tr.vdr_exchange(send_words, n_response)

            if mask_words is None:
                return responses[:n_response]

            # Search for a matching window in the response stream
            for offset in range(len(responses) - n_response + 1):
                window = responses[offset:offset + n_response]
                ok = True
                for i, (payload, sop, eop) in enumerate(window):
                    if i < len(mask_words) and mask_words[i] is not None:
                        exp_payload, exp_mask = mask_words[i]
                        if (payload & exp_mask) != (exp_payload & exp_mask):
                            ok = False
                            break
                if ok:
                    return window

        raise SdmError(f"SDM VDR transaction failed after {retries} retries")

    async def transact(self, vir_words, vdr_send, vdr_n_response,
                       vdr_mask=None, retries=75):
        """Complete SDM transaction: VIR command write + VDR data exchange."""
        await self._tr.vir_write(vir_words)
        return await self._vdr_transact(
            vdr_send, vdr_n_response, vdr_mask, retries)

    # ------------------------------------------------------------------
    # SDM commands
    # ------------------------------------------------------------------

    async def sync(self, sync2_vir=None, sync2_vdr=None, sync2_mask=None):
        """Synchronize with the SDM.

        Phase 1: flush/reset.
        Phase 2 (optional): device-specific handshake.
        """
        p = self._tr.pack_word

        # Phase 1: flush
        flush_vir = [p(0xC0000000), p(0x80000000)]
        ack_vdr = [p(0x00000000, sop=True, eop=True)]
        ack_mask = [(0x00000000, self.BUSY_MASK)]
        await self.transact(flush_vir, ack_vdr, 1, ack_mask)

        # Phase 2: handshake
        if sync2_vir is not None:
            await self.transact(
                sync2_vir, sync2_vdr,
                len(sync2_vdr), sync2_mask)

    async def command(self, cmd_id, sop=True, eop=False, retries=75):
        """Send a simple SDM command (bit[31]=1, cmd_id in [7:0]).

        Returns single VDR ack response payload.
        """
        p = self._tr.pack_word
        payload = 0x80000000 | (cmd_id & 0xFF)
        vir = [p(payload, sop=sop, eop=eop)]
        ack = [p(0, sop=True, eop=True)]
        ack_mask = [(0x00000000, self.BUSY_MASK)]
        resp = await self.transact(vir, ack, 1, ack_mask, retries)
        return resp[0][0]

    async def status(self):
        """Query SDM status (5 VDR response words).

        Returns list of (payload, sop, eop) tuples.
        """
        p = self._tr.pack_word
        vir = [p(0x80000001)]  # cmd_id=1, no SOP
        await self._tr.vir_write(vir)
        return await self._tr.vdr_exchange(
            [p(0)] * 5, 5)


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
    _STREAM_STATUS_RETRIES = 6000

    # Subclass must set these for sync phase 2
    _SYNC2_VIR = None
    _SYNC2_VDR = None
    _SYNC2_MASK = None

    def _build_sdm(self):
        """Build the SDM mailbox stack."""
        transport = SdmJtagTransport(self, vir_ir=int(self.VIR),
                                     vdr_ir=int(self.VDR))
        return SdmMailbox(transport)

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

        Implements adaptive chunk sizing with status checks.
        """
        chunk_size = self._STREAM_INITIAL_CHUNK
        sent_bits = 0
        first = True

        header_bs = BitString(
            self._STREAM_HEADER.to_bytes(8, 'little'),
            self._STREAM_HEADER_BITS)

        while sent_bits < total_bits:
            # Status check
            done, error, progress, fifo_free = await self._stream_status_check(
                request_data=(first or done),
                start_config=first,
                enable=first,
            )
            first = False

            if error:
                raise SdmError(
                    f"SDM error during streaming at {sent_bits}/{total_bits}")

            if progress >= total_bits:
                break

            if done and sent_bits > 0:
                # SDM stalled — shrink chunks
                chunk_size = max(self._STREAM_INITIAL_CHUNK, chunk_size // 2)
            elif not done and chunk_size < total_bits:
                chunk_size = min(chunk_size * 2, total_bits)

            n = min(chunk_size, total_bits - sent_bits)
            data_slice = BitString(
                bytes(data[sent_bits // 8:(sent_bits + n + 7) // 8]), n)
            trailer = BitString(0, 1)
            frame = header_bs + data_slice + trailer

            await self.ir(int(self.CONFIG), dr_length=len(frame))(
                frame, read_tdo=False)
            await self.run(16)

            sent_bits += n

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
        await sdm.sync(self._SYNC2_VIR, self._SYNC2_VDR, self._SYNC2_MASK)

        self.logger.trace("Requesting configuration...")
        await sdm.command(0x01, sop=True, retries=300)

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
}


@Tap.db.register(*_AGILEX5_PARTS.keys())
class Agilex5E(Agilex5):
    """Agilex 5 E-series."""

    def __init__(self, interface, idcode, **kw):
        name = _AGILEX5_PARTS.get(idcode, f"Agilex5-0x{idcode:08x}")
        super().__init__(interface, idcode, name=name, **kw)

        p = SdmJtagTransport.pack_word
        # Sync2 signatures for configuration mode (from STAPL dj161)
        self._SYNC2_VIR = [
            p(0x7C000400, sop=True),
            p(0x8F812007, eop=True),
        ]
        self._SYNC2_VDR = [
            p(0xF0001000, sop=True),
            p(0x3E04801E, sop=True, eop=True),
        ]
        self._SYNC2_MASK = [
            (0xF0001000, 0xFF7FFFFF),
            (0x3E04801E, 0xFFFFFFFF),
        ]
