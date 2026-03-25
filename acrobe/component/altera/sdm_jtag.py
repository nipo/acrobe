"""SDM (Secure Device Manager) Avalon-ST transport over JTAG Virtual IR/VDR.

This module implements the physical transport layer for communicating
with Altera/Intel SDM on Agilex devices via JTAG.

The SDM exposes two virtual JTAG registers:
  - VIR (IR 0x201): command write channel (host → SDM)
  - VDR (IR 0x202): data exchange channel (bidirectional)

Both are single-word shift registers: each DR scan of 34 bits shifts
one word in (TDI) and one word out (TDO). Words use Avalon-ST
framing:

    [33:2] = 32-bit payload
    [1]    = EOP (end of packet)
    [0]    = SOP (start of packet)

VIR has FIFO flow control: read VIR DR to get used-slot count in
bits [11:0], then push command words when slots are free.

VDR is purely shift-register-based: the SDM loads its response into
the register asynchronously. Each scan captures whatever the SDM has
placed there since the last scan.

See NOTES.md for full protocol documentation.
"""

from ...bitstring import BitString


class SdmJtagTransport:
    """Low-level Avalon-ST word transport over JTAG VIR/VDR.

    Provides word-level send/recv on top of a Tap instance.
    The 34-bit Avalon-ST framing (SOP/EOP) is exposed to the caller.
    """

    WORD_BITS = 34
    WORD_MASK = (1 << 34) - 1
    FIFO_DEPTH_BITS = 12
    FIFO_DEPTH_MAX = 16

    SOP = 0x1
    EOP = 0x2

    def __init__(self, tap, vir_ir=0x201, vdr_ir=0x202):
        self._tap = tap
        self._vir_ir = vir_ir
        self._vdr_ir = vdr_ir

    @staticmethod
    def pack_word(payload, sop=False, eop=False):
        """Pack a 32-bit payload into a 34-bit Avalon-ST word."""
        return ((payload & 0xFFFFFFFF) << 2) | (int(eop) << 1) | int(sop)

    @staticmethod
    def unpack_word(raw):
        """Unpack a 34-bit word into (payload, sop, eop)."""
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

    async def vir_write(self, words):
        """Write 34-bit command words to VIR with FIFO flow control.

        Polls VIR DR for free slots, then pushes words one at a time.
        """
        ir = self._vir_ir
        n = len(words)

        # Set IR to VIR
        await self._tap.ir(ir)(read_tdo=False)
        await self._tap.run(16)

        # Poll for FIFO free slots
        for _ in range(50):
            raw = await self._shift_word(ir, 0)
            used = raw & ((1 << self.FIFO_DEPTH_BITS) - 1)
            if self.FIFO_DEPTH_MAX - used >= n:
                break
        else:
            raise RuntimeError("SDM VIR FIFO not ready")

        # Push words one at a time
        for w in words:
            await self._shift_word(ir, w)

    async def vdr_shift(self, word):
        """Shift one 34-bit word through VDR.

        Returns the captured TDO word as (payload, sop, eop).
        """
        raw = await self._shift_word(self._vdr_ir, word)
        return self.unpack_word(raw)

    async def vdr_begin(self):
        """Set IR to VDR. Call before a series of vdr_shift() calls."""
        await self._tap.ir(self._vdr_ir)(read_tdo=False)
        await self._tap.run(16)

    async def vdr_exchange(self, tx_words, rx_count):
        """Send tx_words through VDR, collect rx_count responses.

        Shifts tx_words one at a time, then shifts zeros to flush
        responses. Returns all captured (payload, sop, eop) tuples.

        The caller is responsible for finding meaningful responses in
        the returned list (responses are pipelined — the response to
        word N may appear on scan N+1 or later).
        """
        await self.vdr_begin()
        responses = []

        for w in tx_words:
            raw = await self._shift_word(self._vdr_ir, w)
            responses.append(self.unpack_word(raw))

        # Flush: read remaining responses
        remaining = rx_count + len(tx_words) - len(responses)
        for _ in range(max(0, remaining)):
            raw = await self._shift_word(self._vdr_ir, 0)
            responses.append(self.unpack_word(raw))

        return responses
