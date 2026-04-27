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

import enum
import time
import asyncio
from ...bitstring import BitString
from .sdm import Sdm, TimeoutError
import random

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

    def nop(self) -> asyncio.Future:
        return self._tap.SDM_CMD(self._cmd_tdi(0, CMD_IDLE), read_tdo=False)

    async def _send_frame(self, words):
        """Send a list of 32-bit words as one CMD frame.

        Posts all shifts in batches, only awaiting the last one
        per batch to let the JTAG adapter aggregate.
        """
        tap = self._tap

        tap.SDM_CMD(self._cmd_tdi(0, CMD_IDLE), read_tdo=False)

        # Send command words in batches
        n = len(words)
        framing = CMD_MORE
        for batch_start in range(0, n, _BATCH_SIZE):
            batch_end = min(batch_start + _BATCH_SIZE, n)
            last_future = None
            for i in range(batch_start, batch_end):
                if i == n - 1:
                    #framing = CMD_SINGLE if n == 1 else CMD_LAST
                    framing = CMD_LAST
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
        interval = 0.001

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

            had_rx = False

            # Process results
            for f in futures:
                raw = int(await f) & ((1 << 34) - 1)
                word, framing = self._unpack_rsp(raw)
                
                if framing in [RSP_IDLE, RSP_INVAL]:
                    continue

                had_rx = True
                words.append(word)

                if rsp_count is None:
                    rsp_count = 1 + ((word >> 12) & 0x7FF)

                if framing == RSP_LAST or len(words) >= rsp_count:
                    return words

            if had_rx:
                silent_to_go = max_silent
                interval = 0.001
            elif silent_to_go <= 0:
                raise TimeoutError("No response from SDM")
            else:
                silent_to_go -= 1
                await asyncio.sleep(interval)
                interval *= 2

        return words

    async def _flush(self, nonce = None, max_retries=10):
        for _ in range(max_retries):
            self._tap.SDM_CMD(self._cmd_tdi(0, CMD_IDLE), read_tdo=False)
            self._tap.SDM_CMD(self._cmd_tdi(0, CMD_SINGLE), read_tdo=False)
            self._tap.SDM_CMD(self._cmd_tdi(0, CMD_LAST), read_tdo=False)
            
            for retry in range(32, -1, -1):
                rx = await self._tap.SDM_RSP(0, read_tdo=True)
                word, framing = self._unpack_rsp(int(rx))
                if framing == RSP_LAST:
                    break

            if nonce is None:
                nonce = random.randint(0, 1<<32)
            await self._send_frame([0xf0001001, nonce])
            
            # Drain stale RSP data
            for retry in range(32, -1, -1):
                if not retry:
                    raise TimeoutError("Unable to sync")

                rx = await self._tap.SDM_RSP(0, read_tdo=True)
                word, framing = self._unpack_rsp(int(rx))
                if word & 0xfffff000 != 0xf0001000 or framing != RSP_MORE:
                    continue
                rx = await self._tap.SDM_RSP(0, read_tdo=True)
                word, framing = self._unpack_rsp(int(rx))
                if word == nonce and framing == RSP_LAST:
                    return
    
    async def sync(self, nonce=None):
        """SDM sync with flush phase before the SYNC command."""

        if hasattr(self._tap, 'SDM_WAKEUP'):
            await self._tap.SDM_WAKEUP(None)
            for _ in range(20):
                await self._tap.run(512)
                await asyncio.sleep(512e-6)

        await self._flush(nonce = nonce)
        await self._tap.run(32)

class SdmJtagMixin:
    class VoltageChannel(enum.IntEnum):
        pass

    async def child_spawn(self, name):
        if name == "sdm":
            return Agilex5SdmJtag(self)
