import asyncio
import struct

from ...protocol.jtag import Tap, Dr, Instruction
from ..fpga import JtagSramFpga
from ...bitstring import BitString


class SdmError(Exception):
    """Raised when SDM communication fails."""


class Agilex5(Tap, JtagSramFpga):
    """Altera/Intel Agilex 5 FPGA with SDM (Secure Device Manager).

    Unlike Cyclone 10 LP which uses direct JTAG bitstream shift,
    Agilex 5 communicates through an SDM mailbox over Virtual JTAG.
    All configuration and flash operations go through this mailbox.

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

    # Status register
    CONF_DONE_BIT = 13

    # SDM protocol constants
    _SDM_WORD_BITS = 34
    _SDM_FIFO_POLL_BITS = 12

    # Bitstream streaming constants
    _STREAM_HEADER = 0xA17E2A00_FFFFFFFF
    _STREAM_HEADER_BITS = 64
    _STREAM_TRAILER_BITS = 1
    _STREAM_INITIAL_CHUNK = 32768
    _STREAM_STATUS_RETRIES = 6000

    # Sync signatures (device-specific, from STAPL dj161)
    _SYNC2_VIR = None   # Subclass must set
    _SYNC2_VDR = None   # Subclass must set
    _SYNC2_MASK = None  # Subclass must set

    # ------------------------------------------------------------------
    # SDM transport layer
    # ------------------------------------------------------------------

    def _pack_sdm_word(self, payload, sop=False, eop=False):
        """Pack a 32-bit payload into a 34-bit Avalon-ST word."""
        return (payload << 2) | (int(eop) << 1) | int(sop)

    def _unpack_sdm_word(self, word):
        """Unpack a 34-bit word into (payload, sop, eop)."""
        return (word >> 2) & 0xFFFFFFFF, bool(word & 1), bool(word & 2)

    def _pack_sdm_words(self, words):
        """Pack list of 34-bit words into a BitString."""
        total_bits = len(words) * self._SDM_WORD_BITS
        val = 0
        for i, w in enumerate(words):
            val |= (w & 0x3FFFFFFFF) << (i * self._SDM_WORD_BITS)
        data = val.to_bytes((total_bits + 7) // 8, 'little')
        return BitString(data, total_bits)

    def _unpack_sdm_response(self, tdo_bytes, n_words):
        """Unpack DR capture into list of (payload, sop, eop)."""
        val = int.from_bytes(tdo_bytes, 'little')
        words = []
        for i in range(n_words):
            w = (val >> (i * self._SDM_WORD_BITS)) & 0x3FFFFFFFF
            words.append(self._unpack_sdm_word(w))
        return words

    async def _sdm_vir_write(self, words):
        """Write command words to the SDM via VIR.

        Polls VDR for FIFO free slots, then sends command words.
        """
        n_words = len(words)
        tdi = self._pack_sdm_words(words)

        # Set IR to VIR
        await self.ir(int(self.VIR))(read_tdo=False)
        await self.run(16)

        # Poll for FIFO free slots
        # (In a single-device chain, DR = SDM_WORD_BITS)
        for _ in range(50):
            zero_tdi = BitString(0, self._SDM_WORD_BITS)
            result = await self.ir(int(self.VDR), dr_length=self._SDM_WORD_BITS)(
                zero_tdi, read_tdo=True)
            await self.run(16)
            val = int(result) & ((1 << self._SDM_FIFO_POLL_BITS) - 1)
            free_slots = 16 - val
            if free_slots >= n_words:
                break
        else:
            raise SdmError("SDM VIR FIFO not ready (timeout)")

        # Send command words
        await self.ir(int(self.VDR), dr_length=len(tdi))(tdi, read_tdo=False)
        await self.run(16)

    async def _sdm_vdr_exchange(self, send_words, n_response,
                                mask_words=None, retries=75):
        """Exchange data words via VDR.

        Sends send_words, then reads n_response words.
        If mask_words is provided, checks responses against expected values.
        Returns list of (payload, sop, eop) tuples.
        """
        # Set IR to VDR
        await self.ir(int(self.VDR))(read_tdo=False)
        await self.run(16)

        total_bits = n_response * self._SDM_WORD_BITS
        send_tdi = self._pack_sdm_words(send_words)

        for attempt in range(retries):
            # Send and capture
            zero_tdi = BitString(0, total_bits)
            result = await self.ir(int(self.VDR), dr_length=total_bits)(
                zero_tdi, read_tdo=True)
            await self.run(16)

            result_bytes = bytes(result.data[:(total_bits + 7) // 8])
            response = self._unpack_sdm_response(result_bytes, n_response)

            if mask_words is None:
                return response

            # Check against mask
            ok = True
            for i, (payload, sop, eop) in enumerate(response):
                if i < len(mask_words) and mask_words[i] is not None:
                    exp_payload, exp_mask = mask_words[i]
                    if (payload & exp_mask) != (exp_payload & exp_mask):
                        ok = False
                        break
            if ok:
                return response

        raise SdmError(f"SDM VDR exchange failed after {retries} retries")

    async def _sdm_transact(self, vir_words, vdr_send, vdr_n_response,
                            vdr_mask=None, retries=75):
        """Complete SDM transaction: VIR write + VDR exchange."""
        await self._sdm_vir_write(vir_words)
        return await self._sdm_vdr_exchange(
            vdr_send, vdr_n_response, vdr_mask, retries)

    # ------------------------------------------------------------------
    # SDM command layer
    # ------------------------------------------------------------------

    async def sdm_sync(self):
        """Synchronize with the SDM.

        Two-phase sync: flush + handshake.
        """
        # Phase 1: flush/reset
        self.logger.trace("SDM sync phase 1...")
        flush_vir = [
            self._pack_sdm_word(0xC0000000),
            self._pack_sdm_word(0x80000000),
        ]
        ack_vdr = [self._pack_sdm_word(0x00000000, sop=True, eop=True)]
        ack_mask = [(0x00000000, 0xFF7FFFFF)]
        await self._sdm_transact(flush_vir, ack_vdr, 1, ack_mask, retries=75)

        # Phase 2: handshake (device-specific signature)
        if self._SYNC2_VIR is not None:
            self.logger.trace("SDM sync phase 2...")
            await self._sdm_transact(
                self._SYNC2_VIR, self._SYNC2_VDR,
                len(self._SYNC2_VDR), self._SYNC2_MASK, retries=75)

    async def sdm_command(self, cmd_id, sop=True, eop=False, retries=75):
        """Send a simple SDM command (bit[31]=1, cmd_id in [7:0]).

        Returns single VDR ack response payload.
        """
        payload = 0x80000000 | (cmd_id & 0xFF)
        vir = [self._pack_sdm_word(payload, sop=sop, eop=eop)]
        ack = [self._pack_sdm_word(0x00000000, sop=True, eop=True)]
        ack_mask = [(0x00000000, 0xFF7FFFFF)]
        resp = await self._sdm_transact(vir, ack, 1, ack_mask, retries)
        return resp[0][0]  # payload

    async def sdm_status(self, retries=75):
        """Query SDM status (5 VDR response words).

        Returns list of 5 (payload, sop, eop) tuples.
        """
        payload = 0x80000000 | 0x01
        vir = [self._pack_sdm_word(payload, sop=False, eop=False)]
        # No mask — just read
        await self._sdm_vir_write(vir)

        # Read 5 words
        total_bits = 5 * self._SDM_WORD_BITS
        zero_tdi = BitString(0, total_bits)
        await self.ir(int(self.VDR))(read_tdo=False)
        await self.run(16)
        result = await self.ir(int(self.VDR), dr_length=total_bits)(
            zero_tdi, read_tdo=True)
        await self.run(16)
        result_bytes = bytes(result.data[:(total_bits + 7) // 8])
        return self._unpack_sdm_response(result_bytes, 5)

    # ------------------------------------------------------------------
    # Bitstream streaming
    # ------------------------------------------------------------------

    async def _stream_status_check(self, request_data=False,
                                   start_config=False, enable=False):
        """Check config status via IR 0x208 (CONFIG_STATUS_VJ).

        Returns (done, error, progress_bits, fifo_free).
        """
        dr_bits = self._SDM_WORD_BITS + 3  # 37 bits for single device
        tdi_val = 0
        if request_data:
            tdi_val |= 1
        if start_config:
            tdi_val |= 2
        if enable:
            tdi_val |= 4

        tdi = BitString(tdi_val, dr_bits)
        await self.ir(int(self.CONFIG_STATUS_VJ))(read_tdo=False)
        await self.run(16)
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

        Implements adaptive chunk sizing with status checks between chunks.
        """
        chunk_size = self._STREAM_INITIAL_CHUNK
        max_chunk = total_bits  # will be capped by SDM flow control
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
                raise SdmError(f"SDM error during streaming at {sent_bits}/{total_bits} bits")

            if done and sent_bits > 0:
                # SDM stalled, reduce chunk size
                if chunk_size > self._STREAM_INITIAL_CHUNK:
                    chunk_size //= 2
            elif not done:
                # SDM keeping up, increase chunk size
                chunk_size = min(chunk_size * 2, max_chunk)

            if progress >= total_bits:
                break

            # Compute this chunk
            remaining = total_bits - sent_bits
            n = min(chunk_size, remaining)

            # Build DR frame: [header 64] [data N] [trailer 1]
            data_slice = BitString(
                bytes(data[sent_bits // 8:(sent_bits + n + 7) // 8]),
                n)
            trailer = BitString(0, self._STREAM_TRAILER_BITS)

            frame = header_bs + data_slice + trailer
            total_dr = len(frame)

            # Shift
            await self.ir(int(self.CONFIG), dr_length=total_dr)(
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
        """Load bitstream into Agilex 5 via SDM.

        The program should contain a single segment with bitstream data
        (e.g. from a JIC/RBF file suitable for this device).
        """
        if len(program) != 1:
            raise ValueError("Bitstream programming requires exactly one segment")

        blob = program[0].data
        total_bits = len(blob) * 8

        # Sync with SDM
        self.logger.trace("Synchronizing with SDM...")
        await self.sdm_sync()

        # Request configuration
        self.logger.trace("Requesting configuration...")
        await self.sdm_command(0x01, sop=True, retries=300)

        # Stream bitstream
        self.logger.trace("Streaming %d bits of bitstream...", total_bits)
        with self.progress("config", len(blob), unit="B"):
            await self._stream_bitstream(blob, total_bits)

        # Poll for completion
        self.logger.trace("Waiting for CONF_DONE...")
        for attempt in range(15):
            await asyncio.sleep(0.1)
            status = await self.sdm_status(retries=75)
            # Check if all response words matched (simple heuristic:
            # word[1] payload should indicate completion)
            self.logger.protocol("SDM status: %s", status)
            # For now, check word 0 for errors
            if len(status) >= 2:
                w1_payload = status[1][0]
                if w1_payload == 0x10000000:
                    self.logger.note("Configuration complete")
                    return

        raise SdmError("Configuration timeout: CONF_DONE not asserted after 1.5s")

    async def erase(self):
        # Agilex 5 is SDM-managed. A new load overwrites.
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

    # Sync phase 2 signature for configuration mode (from STAPL dj161)
    _SYNC2_VIR = None  # decoded below in __init_subclass__
    _SYNC2_VDR = None
    _SYNC2_MASK = None

    def __init__(self, interface, idcode, **kw):
        name = _AGILEX5_PARTS.get(idcode, f"Agilex5-0x{idcode:08x}")
        super().__init__(interface, idcode, name=name, **kw)

        # Sync2 signatures for this device (from STAPL dj161 transaction 2)
        p = self._pack_sdm_word
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
