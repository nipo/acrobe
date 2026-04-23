"""SDM (Secure Device Manager) JTAG transport.

Implements the physical transport layer for communicating with Altera
SDM on Agilex devices via JTAG.

The SDM exposes two JTAG instructions:
  - SDM_CMD (IR 0x201): command FIFO (host → SDM)
  - SDM_RSP (IR 0x202): response FIFO (SDM → host)

Each DR scan shifts 34 bits. The 34-bit format is asymmetric:

  CMD direction (host → SDM, TDI, framing at MSB [33:32]):
    [31:0]  = 32-bit command/data word
    [33:32] = framing enum:
        00 = idle (no data)
        01 = valid, more words follow
        10 = valid, last word of frame
        11 = single-word frame (flush/reset)

  RSP direction (SDM → host, TDO, framing at LSB [1:0]):
    [33:2]  = 32-bit response word
    [1:0]   = framing enum:
        00 = idle (no data)
        01 = valid, more words follow
        11 = valid, last word of frame
        10 = reserved/unknown

SDM command word format (32-bit):
    [31:28] = upper (family-specific, 0xF for SYNC)
    [27:24] = ID tag (echoed in response)
    [23]    = 0
    [22:12] = length (argument words following header)
    [11]    = 0
    [10:0]  = opcode

SDM response header format (32-bit):
    [31:28] = upper
    [27:24] = ID tag (echoed from command)
    [23]    = 0
    [22:12] = length (data words following header)
    [11]    = 0
    [10:0]  = error code (0 = OK)

See NOTES_SDM_REVISED.md for full protocol documentation.
"""

from ...bitstring import BitString
from ...protocol.jtag import CaptureIr, CaptureDr, Shift, Run


# CMD framing values [33:32]
CMD_IDLE = 0
CMD_MORE = 1   # valid, more words follow
CMD_LAST = 2   # valid, last word
CMD_SINGLE = 3 # single-word frame (flush)

# RSP framing values [1:0]
RSP_IDLE = 0
RSP_MORE = 1   # valid, more words follow
RSP_LAST = 3   # valid, last word


class SdmJtagTransport:
    """SDM command/response transport over JTAG.

    Uses the raw JTAG interface (CaptureIr/CaptureDr/Shift/Run ops)
    directly, not through a Tap abstraction.
    """

    SDM_CMD_IR = 0x201
    SDM_RSP_IR = 0x202

    def __init__(self, interface, ir_width=10):
        self._iface = interface
        self._ir_width = ir_width

    # ------------------------------------------------------------------
    # Low-level JTAG helpers
    # ------------------------------------------------------------------

    async def _ir_load(self, ir_value):
        """Load an IR value via JTAG."""
        tdi = BitString(
            ir_value.to_bytes((self._ir_width + 7) // 8, 'little'),
            self._ir_width)
        await self._iface.post(CaptureIr())
        await self._iface.post(Shift(tdi, read_tdo=False))
        await self._iface.post(Run(16))

    async def _dr_shift_34(self, raw_34):
        """Shift a 34-bit DR value in, return 34-bit TDO value."""
        tdi = BitString(raw_34.to_bytes(5, 'little'), 34)
        await self._iface.post(CaptureDr())
        shift = Shift(tdi, read_tdo=True)
        result = await self._iface.post(shift)
        await self._iface.post(Run(16))
        tdo_bytes = bytes(result.tdo.data[:5])
        return int.from_bytes(tdo_bytes, 'little') & ((1 << 34) - 1)

    # ------------------------------------------------------------------
    # CMD packing / RSP unpacking
    # ------------------------------------------------------------------

    @staticmethod
    def pack_cmd(word, framing=CMD_IDLE):
        """Pack a 32-bit word with CMD framing into a 34-bit value.

        CMD format: [31:0] = word, [33:32] = framing.
        """
        return (word & 0xFFFFFFFF) | (framing << 32)

    @staticmethod
    def unpack_rsp(raw_34):
        """Unpack a 34-bit RSP value into (word, framing).

        RSP format: [33:2] = word, [1:0] = framing.
        """
        return (raw_34 >> 2) & 0xFFFFFFFF, raw_34 & 0x3

    # ------------------------------------------------------------------
    # SDM command / response
    # ------------------------------------------------------------------

    async def send_cmd_frame(self, words):
        """Send a command frame (list of 32-bit words) to SDM.

        Applies correct framing: first word gets CMD_MORE (or CMD_LAST
        if single word), middle words get CMD_MORE, last gets CMD_LAST.
        """
        await self._ir_load(self.SDM_CMD_IR)

        # Flush: shift a zero word first to clear any stale state
        await self._dr_shift_34(self.pack_cmd(0, CMD_IDLE))

        n = len(words)
        for i, w in enumerate(words):
            if n == 1:
                framing = CMD_LAST
            elif i < n - 1:
                framing = CMD_MORE
            else:
                framing = CMD_LAST
            await self._dr_shift_34(self.pack_cmd(w, framing))

    async def recv_rsp_frame(self, max_words=32, timeout_shifts=64):
        """Receive a response frame from SDM.

        Reads words until LAST framing is seen or max_words reached.
        Returns list of 32-bit response words (excluding idle words).

        timeout_shifts: max number of DR shifts before giving up
        (includes idle words that don't count toward max_words).
        """
        await self._ir_load(self.SDM_RSP_IR)

        words = []
        for _ in range(timeout_shifts):
            raw = await self._dr_shift_34(self.pack_cmd(0, CMD_IDLE))
            word, framing = self.unpack_rsp(raw)

            if framing == RSP_IDLE:
                continue  # no data yet

            words.append(word)

            if framing == RSP_LAST or len(words) >= max_words:
                break

        return words

    async def command(self, opcode, args=None, id_tag=0, upper=0,
                      max_response=32):
        """Send an SDM command and return the response.

        Builds the command header, sends the frame, reads the response.

        Returns (error_code, response_words) where response_words
        does NOT include the header.
        """
        # Build command header
        header = (opcode & 0x7FF)
        n_args = len(args) if args else 0
        header |= (n_args & 0x7FF) << 12
        header |= (id_tag & 0xF) << 24
        header |= (upper & 0xF) << 28

        # Build command frame
        frame = [header]
        if args:
            frame.extend(args)

        await self.send_cmd_frame(frame)

        # Read response
        rsp = await self.recv_rsp_frame(max_words=max_response + 1)

        if not rsp:
            return None, []

        # Parse response header
        rsp_header = rsp[0]
        error_code = rsp_header & 0x7FF
        rsp_length = (rsp_header >> 12) & 0x7FF
        rsp_id = (rsp_header >> 24) & 0xF

        return error_code, rsp[1:]

    async def sync(self, nonce=0xDEADBEEF):
        """Perform SDM sync handshake.

        Phase 1: Flush (single-word frame of zeros).
        Phase 2: SYNC command (opcode 1) with a nonce word.
                 SDM echoes the nonce back to prove it's alive.
                 The nonce value is arbitrary.

        Returns the echoed nonce, or raises on failure.
        """
        # Phase 1: Flush
        await self._ir_load(self.SDM_CMD_IR)
        await self._dr_shift_34(self.pack_cmd(0, CMD_IDLE))
        await self._dr_shift_34(self.pack_cmd(0, CMD_SINGLE))
        await self._dr_shift_34(self.pack_cmd(0, CMD_LAST))

        # Read flush response (should be idle/zero)
        await self._ir_load(self.SDM_RSP_IR)
        await self._dr_shift_34(self.pack_cmd(0, CMD_IDLE))

        # Phase 2: SYNC command (opcode 1) with nonce
        error_code, data = await self.command(
            opcode=1, args=[nonce], upper=0xF, max_response=2)

        if error_code is None:
            raise SdmError("SDM sync: no response")
        if error_code != 0:
            raise SdmError(f"SDM sync: error {error_code}")
        if not data or data[0] != nonce:
            raise SdmError(
                f"SDM sync: nonce mismatch "
                f"(sent {nonce:#010x}, got {data[0] if data else 'nothing'})")

        return data[0]


class SdmError(Exception):
    """Raised when SDM communication fails."""
