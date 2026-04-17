"""SDM (Secure Device Manager) transport over JTAG Virtual IR/VDR.

This module implements the physical transport layer for communicating
with Altera/Intel SDM on Agilex devices via JTAG.

The SDM exposes two virtual JTAG registers:
  - VIR (IR 0x201): command channel (host → SDM)
  - VDR (IR 0x202): data exchange channel (bidirectional)

Both are single-word shift registers: each DR scan of 34 bits shifts
one word in (TDI) and one word out (TDO).

34-bit word format (asymmetric by direction):

  VIR command words (host → SDM):
    [33:2] = 32-bit command payload
    [1]    = LAST (end of multi-word command / release)
    [0]    = FIRST (start of command / write request)

    Encoding:
      00 = read-only query (no new command for SDM)
      01 = first word of write command (or single-word request)
      10 = last word of multi-word command
      11 = complete single-word write+close command

  VDR response words (SDM → host):
    [33:2] = 32-bit response payload
    [0]    = VALID (response data present)
    [1]    = LAST (end of response packet)

VIR has FIFO flow control: read VIR DR to get used-slot count in
bits [11:0], then push command words when slots are free.

VDR is a shift register: the SDM loads its response asynchronously.
Each scan captures the current register content.

See NOTES.md for full protocol documentation.
"""

from ...bitstring import BitString


class SdmJtagTransport:
    """34-bit word transport over JTAG VIR/VDR to the SDM."""

    WORD_BITS = 34
    WORD_MASK = (1 << 34) - 1
    FIFO_DEPTH_BITS = 12
    FIFO_DEPTH_MAX = 16

    # Command modifier bits (VIR direction)
    FIRST = 0x1
    LAST = 0x2

    def __init__(self, tap, vir_ir=0x201, vdr_ir=0x202):
        self._tap = tap
        self._vir_ir = vir_ir
        self._vdr_ir = vdr_ir

    @staticmethod
    def pack_word(payload, first=False, last=False):
        """Pack a 32-bit payload with modifier bits into a 34-bit word.

        For VIR commands: first=write/request, last=end/release.
        For VDR sends: first/last set as needed for expected responses.
        """
        return ((payload & 0xFFFFFFFF) << 2) | (int(last) << 1) | int(first)

    @staticmethod
    def unpack_word(raw):
        """Unpack a 34-bit word.

        Returns (payload, valid_or_first, last).
        For VDR responses: valid_or_first indicates response data present.
        """
        return (raw >> 2) & 0xFFFFFFFF, bool(raw & 1), bool(raw & 2)

    async def _shift_word(self, ir, word):
        """Shift one 34-bit word through the DR under the given IR value.

        Returns the captured TDO (previous DR content).
        """
        tdi = BitString(
            (word & self.WORD_MASK).to_bytes(5, 'little'),
            self.WORD_BITS)
        result = await self._tap.ir(ir, dr_length=self.WORD_BITS)(
            tdi, read_tdo=True)
        await self._tap.run(16)
        return int(result) & self.WORD_MASK

    async def vir_write(self, words, atomic=True):
        """Write command words to VIR.

        If atomic=True (default): packs all words into a single DR
        scan of len(words) × 34 bits.  The SDM processes SPI commands
        atomically and requires this mode.

        If atomic=False: shifts words one at a time (34-bit DR each).
        This was the original STAPL j89 behavior and works for
        sync/config commands.

        Polls VIR for FIFO space first.
        """
        ir = self._vir_ir
        n = len(words)

        # Set IR to VIR and settle
        await self._tap.ir(ir)(read_tdo=False)
        await self._tap.run(16)

        # Poll for FIFO free slots (single 34-bit read)
        for _ in range(50):
            raw = await self._shift_word(ir, 0)
            used = raw & ((1 << self.FIFO_DEPTH_BITS) - 1)
            if self.FIFO_DEPTH_MAX - used >= n:
                break
        else:
            raise RuntimeError("SDM VIR FIFO not ready")

        if atomic:
            # Pack all words into one DR scan
            total_bits = n * self.WORD_BITS
            val = 0
            for i, w in enumerate(words):
                val |= (w & self.WORD_MASK) << (i * self.WORD_BITS)
            data = val.to_bytes((total_bits + 7) // 8, 'little')
            tdi = BitString(data, total_bits)
            await self._tap.ir(ir, dr_length=total_bits)(tdi, read_tdo=False)
            await self._tap.run(16)
        else:
            # Push words one at a time
            for w in words:
                await self._shift_word(ir, w)

    async def vdr_shift(self, word):
        """Shift one 34-bit word through VDR.

        Returns (payload, valid, last).
        """
        raw = await self._shift_word(self._vdr_ir, word)
        return self.unpack_word(raw)

    async def vdr_begin(self):
        """Set IR to VDR and settle. Call before vdr_shift() series."""
        await self._tap.ir(self._vdr_ir)(read_tdo=False)
        await self._tap.run(16)

    async def vdr_read(self, count):
        """Read `count` response words from VDR.

        Shifts zeros through VDR DR and captures the SDM's responses.
        The SDM loads response data into the shift register; the host
        always sends zeros.

        Returns list of (payload, valid, last) tuples.
        """
        await self.vdr_begin()
        responses = []
        for _ in range(count):
            raw = await self._shift_word(self._vdr_ir, 0)
            responses.append(self.unpack_word(raw))
        return responses
