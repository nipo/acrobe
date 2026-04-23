"""SDM transport over JTAG.

Implements SDM frame I/O using the two SDM JTAG instructions:
  - SDM_CMD (IR 0x201): command FIFO, host → SDM
  - SDM_RSP (IR 0x202): response FIFO, SDM → host

34-bit DR format:
  CMD (TDI): [31:0] = word, [33:32] = framing (00=idle, 01=more, 10=last, 11=single)
  RSP (TDO): [1:0] = framing (00=idle, 01=more, 11=last), [33:2] = word
"""

from ...bitstring import BitString
from ...protocol.jtag import CaptureIr, CaptureDr, Shift, Run
from .sdm import Sdm, SdmTimeoutError

# CMD framing values [33:32]
CMD_IDLE = 0
CMD_MORE = 1
CMD_LAST = 2
CMD_SINGLE = 3

# RSP framing values [1:0]
RSP_IDLE = 0
RSP_MORE = 1
RSP_LAST = 3


class SdmJtag(Sdm):
    """SDM transport over JTAG SDM_CMD/SDM_RSP instructions.

    Takes a raw JTAG interface (Batcher) directly.
    """

    SDM_CMD_IR = 0x201
    SDM_RSP_IR = 0x202

    def __init__(self, interface, ir_width=10):
        super().__init__()
        self._iface = interface
        self._ir_width = ir_width

    # ------------------------------------------------------------------
    # Low-level JTAG
    # ------------------------------------------------------------------

    async def _ir_load(self, ir_value):
        """Load an IR value."""
        tdi = BitString(
            ir_value.to_bytes((self._ir_width + 7) // 8, 'little'),
            self._ir_width)
        await self._iface.post(CaptureIr())
        await self._iface.post(Shift(tdi, read_tdo=False))
        await self._iface.post(Run(16))

    async def _dr_shift_34(self, raw_34):
        """Shift 34 bits, return 34-bit TDO."""
        tdi = BitString(raw_34.to_bytes(5, 'little'), 34)
        await self._iface.post(CaptureDr())
        shift = Shift(tdi, read_tdo=True)
        result = await self._iface.post(shift)
        await self._iface.post(Run(16))
        return int.from_bytes(bytes(result.tdo.data[:5]), 'little') \
            & ((1 << 34) - 1)

    # ------------------------------------------------------------------
    # Packing
    # ------------------------------------------------------------------

    @staticmethod
    def _pack_cmd(word, framing):
        return (word & 0xFFFFFFFF) | (framing << 32)

    @staticmethod
    def _unpack_rsp(raw_34):
        return (raw_34 >> 2) & 0xFFFFFFFF, raw_34 & 0x3

    # ------------------------------------------------------------------
    # SDM frame I/O
    # ------------------------------------------------------------------

    async def do_io(self, cmd):
        """Send a command frame and receive a response frame."""
        await self._send_frame(cmd)
        return await self._recv_frame()

    async def _send_frame(self, words):
        """Send a list of 32-bit words as one CMD frame."""
        await self._ir_load(self.SDM_CMD_IR)

        # Flush: idle word first
        await self._dr_shift_34(self._pack_cmd(0, CMD_IDLE))

        n = len(words)
        for i, w in enumerate(words):
            if n == 1:
                framing = CMD_LAST
            elif i < n - 1:
                framing = CMD_MORE
            else:
                framing = CMD_LAST
            await self._dr_shift_34(self._pack_cmd(w, framing))

    async def _recv_frame(self, max_silent=10):
        """Receive one response frame from RSP channel.

        Shifts zeros into RSP DR until we see valid framing.
        Collects words until LAST framing or timeout.
        """
        await self._ir_load(self.SDM_RSP_IR)

        words = []
        rsp_count = None
        silent_streak = 0

        while True:
            raw = await self._dr_shift_34(self._pack_cmd(0, CMD_IDLE))
            word, framing = self._unpack_rsp(raw)

            if framing == RSP_IDLE:
                silent_streak += 1
                if silent_streak > max_silent:
                    if not words:
                        raise SdmTimeoutError("No response from SDM")
                    break
                continue

            silent_streak = 0
            words.append(word)

            # First word: extract expected count from header
            if rsp_count is None:
                rsp_count = 1 + ((word >> 12) & 0x7FF)

            if framing == RSP_LAST or len(words) >= rsp_count:
                break

        return words

    async def sync(self, nonce=0xDEADBEEF):
        """SDM sync with flush phase before the SYNC command."""
        # Phase 1: Flush
        await self._ir_load(self.SDM_CMD_IR)
        await self._dr_shift_34(self._pack_cmd(0, CMD_IDLE))
        await self._dr_shift_34(self._pack_cmd(0, CMD_SINGLE))
        await self._dr_shift_34(self._pack_cmd(0, CMD_LAST))

        # Drain any stale RSP data
        await self._ir_load(self.SDM_RSP_IR)
        await self._dr_shift_34(self._pack_cmd(0, CMD_IDLE))

        # Phase 2: SYNC via parent
        return await super().sync(nonce)
