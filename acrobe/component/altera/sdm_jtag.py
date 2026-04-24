"""SDM transport over JTAG.

Implements SDM frame I/O using the TAP's SDM_CMD and SDM_RSP
instructions. These must be defined on the Tap class as:

    SDM_CMD = Instruction(0x201, None)
    SDM_RSP = Instruction(0x202, None)

34-bit DR format:
  CMD (TDI): [31:0] = word, [33:32] = framing
             00=idle, 01=more, 10=last, 11=single
  RSP (TDO): [1:0] = framing, [33:2] = word
             00=idle, 01=more, 11=last
"""

import time
import asyncio
from ...bitstring import BitString
from .sdm import Sdm, SdmTimeoutError

# CMD framing values [33:32]
CMD_IDLE = 0
CMD_MORE = 1
CMD_LAST = 2
CMD_SINGLE = 3

# RSP framing values [1:0]
RSP_IDLE = 0
RSP_MORE = 1
RSP_INVAL = 2
RSP_LAST = 3

# Max words per batch (limits how many shifts we post before awaiting)
_BATCH_SIZE = 64


class SdmJtag(Sdm):
    """SDM transport over JTAG SDM_CMD/SDM_RSP instructions.

    Takes a Tap instance that must define SDM_CMD and SDM_RSP
    instructions.
    """

    def __init__(self, tap):
        super().__init__()
        self._tap = tap
        assert hasattr(tap, 'SDM_CMD'), "Tap must define SDM_CMD instruction"
        assert hasattr(tap, 'SDM_RSP'), "Tap must define SDM_RSP instruction"

    # ------------------------------------------------------------------
    # Packing
    # ------------------------------------------------------------------

    @staticmethod
    def _pack_cmd(word, framing):
        return (word & 0xFFFFFFFF) | (framing << 32)

    @staticmethod
    def _unpack_rsp(raw_34):
        return (raw_34 >> 2) & 0xFFFFFFFF, raw_34 & 0x3

    def _cmd_tdi(self, word, framing):
        """Build a 34-bit TDI BitString for a CMD shift."""
        val = self._pack_cmd(word, framing)
        return BitString(val.to_bytes(5, 'little'), 34)

    # ------------------------------------------------------------------
    # SDM frame I/O
    # ------------------------------------------------------------------

    async def do_io(self, cmd):
        """Send a command frame and receive a response frame."""
        await self._send_frame(cmd)
        return await self._recv_frame()

    async def _send_frame(self, words):
        """Send a list of 32-bit words as one CMD frame.

        Posts all shifts in batches, only awaiting the last one
        per batch to let the JTAG adapter aggregate.
        """
        tap = self._tap

        # Flush: idle word (fire and forget)
        tap.SDM_CMD(self._cmd_tdi(0, CMD_IDLE), read_tdo=False)

        # Send command words in batches
        n = len(words)
        for batch_start in range(0, n, _BATCH_SIZE):
            batch_end = min(batch_start + _BATCH_SIZE, n)
            last_future = None
            for i in range(batch_start, batch_end):
                framing = CMD_LAST if i == n - 1 else CMD_MORE
                last_future = tap.SDM_CMD(
                    self._cmd_tdi(words[i], framing), read_tdo=False)
            await last_future

    async def _recv_frame(self, max_silent=10):
        """Receive one response frame from RSP channel.

        Posts batched zero-shifts, reads TDO, collects words
        until LAST framing or timeout.
        """
        tap = self._tap
        words = []
        rsp_count = None
        silent_to_go = max_silent
        zero_tdi = self._cmd_tdi(0, CMD_IDLE)

        while True:
            # Determine how many words to read in this batch
            if rsp_count is not None:
                remaining = rsp_count - len(words)
            else:
                remaining = 1  # start with single reads until we know count
            to_rx = min(max(remaining, 1), _BATCH_SIZE)

            # Post batch of RSP shifts
            futures = []
            for _ in range(to_rx):
                futures.append(tap.SDM_RSP(zero_tdi, read_tdo=True))

            silent_to_go -= 1

            # Process results
            for f in futures:
                raw = int(await f) & ((1 << 34) - 1)
                word, framing = self._unpack_rsp(raw)
                
                if framing == RSP_IDLE:
                    continue

                silent_to_go = max_silent
                words.append(word)

                if rsp_count is None:
                    rsp_count = 1 + ((word >> 12) & 0x7FF)

                if framing == RSP_LAST or len(words) >= rsp_count:
                    return words

            if silent_to_go <= 0:
                raise SdmTimeoutError("No response from SDM")

        return words

    async def _flush(self):
        while True:
            self._tap.SDM_CMD(self._cmd_tdi(0, CMD_IDLE), read_tdo=False)
            self._tap.SDM_CMD(self._cmd_tdi(0, CMD_SINGLE), read_tdo=False)
            self._tap.SDM_CMD(self._cmd_tdi(0, CMD_LAST), read_tdo=False)

            # Drain stale RSP data
            while True:
                rx = await self._tap.SDM_RSP(0, read_tdo=True)
                word, framing = self._unpack_rsp(int(rx))
                if framing in [RSP_MORE, RSP_LAST]:
                    continue
                if framing == RSP_INVAL:
                    break
                if framing == RSP_IDLE:
                    return
    
    async def sync(self, nonce=None):
        """SDM sync with flush phase before the SYNC command."""

        if hasattr(self._tap, 'SDM_WAKEUP'):
            pre = time.time()
            left = 200
            await self._tap.SDM_WAKEUP(None)
            while time.time() < pre + .1 and left > 0:
                await self._tap.run(512)
                left -= 1
        await self._tap.IDCODE(0, read_tdo = True)
                

        # Phase 1: Flush
        await self._flush()
        # Phase 2: SYNC via parent
        return await super().sync(nonce)
