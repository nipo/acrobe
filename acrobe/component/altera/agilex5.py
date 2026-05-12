import asyncio
import enum
import struct

from ...protocol.jtag import Tap, Dr, Instruction, Chain
from ...part_id import PartId
from ..fpga import JtagSramFpga
from ...bitstring import BitString
from ...bitfield import *
from ...node import Node
from .sdm import Sdm, Error, ConfigStatus, Command
from .sdm_jtag import SdmJtagMixin

class Agilex5(Tap, JtagSramFpga, SdmJtagMixin):
    """Altera/Intel Agilex 5 FPGA with SDM.

    All configuration goes through the SDM over JTAG.
    See NOTES_SDM_REVISED.md for protocol documentation.
    """

    class VoltageChannel(enum.IntEnum):
        External0 = 0
        External1 = 1
        Vcc = 2
        VccIoSdm = 3
        VccPt = 4
        VccRCore = 5
        VccHSdm = 6
        VccLSdm = 7
        VccAdc = 9

    irlen = 10
    max_freq = 30e6

    # DR descriptors
    DEVICE_ID = Dr(32)
    BYPASS_REG = Dr(1)
    USER_CODE = Dr(32)
    CHECK_STATUS_REG = Dr(492)

    STATUS2_REG = Dr(128)
    STATUS2 = Instruction(0x17, "STATUS2_REG")
    
    # Core JTAG instructions
    CONFIG = Instruction(0x002, None)
    CONFIG_STATUS_REG = Dr(37)
    CONFIG_STATUS = Instruction(0x208, "CONFIG_STATUS_REG")

    CHECK_STATUS = Instruction(0x004, "CHECK_STATUS_REG")
    IDCODE = Instruction(0x006, "DEVICE_ID")
    USERCODE = Instruction(0x007, "USER_CODE")
    BYPASS = Instruction(0x3FF, "BYPASS_REG")

    # SDM JTAG instructions
    SDM_IO = Dr(34)
    SDM_CMD = Instruction(0x201, "SDM_IO")
    SDM_RSP = Instruction(0x202, "SDM_IO")
    SDM_WAKEUP = Instruction(0x281, None)

    CONF_DONE_BIT = 13

    # Bitstream streaming constants
    _STREAM_HEADER = BitString(0xA17E2A00_FFFFFFFF, 64)
    _STREAM_INITIAL_CHUNK = 32768
    _STREAM_MAX_CHUNK = 524288
    _STREAM_STATUS_RETRIES = 6000

    # ------------------------------------------------------------------
    # SramFpga interface
    # ------------------------------------------------------------------

    async def start(self):
        configured = await self.is_configured()
        self.logger.note("IDCODE: 0x%08x, configured: %s",
                         self.idcode, configured)

    async def load(self, source):
        """Load bitstream into Agilex 5 via SDM.

        The chain geometry changes around this call:

        * ``sdm.config_request()`` tears down the FPGA fabric, which
          on a chip whose previous bitstream brought up the HPS
          debug TAP means that TAP is about to disappear from the
          hardware scan chain. config_request itself completes
          through the SDM at the current geometry; the chain shrink
          becomes observable on the wire only after config_request
          returns (TDO floats until the next TLR). We bracket it
          so the SDM call uses the pre-shrink geometry and the
          following CONFIG_STATUS / stream loop uses the post-shrink
          geometry.
        * A successful HPS-enabled bitstream brings the HPS ARM
          debug TAP back into the chain, but only after a JTAG
          Test-Logic-Reset. After streaming + CONF_DONE we drive
          a TLR via ``Chain.tlr_and_refresh`` to make the new
          neighbour visible. ``post_tlr`` then claims it as gated.
        """
        blob = await source.read(0, source.size)
        total_bits = len(blob) * 8
        chain = self.parent_of_class(Chain)
        my_ctx = chain.context(self)

        sdm = await self.child_summon("sdm")
        await sdm.config_request()

        if my_ctx.gated:
            # config_request just told the chip to tear down user
            # logic, including any HPS that brought a gated TAP
            # online. Right after the SDM returns, the chip is in a
            # transition state where TDO is unreadable — empirically
            # ~2 s on Agilex 5 — until the next JTAG TLR clocks the
            # new visible chain length in. Drive that TLR via
            # `tlr_and_refresh`: it shrinks the software chain to
            # match (the now-absent gated taps drop out via identity
            # matching), and the streaming-phase CONFIG_STATUS
            # shifts that follow are framed for the new geometry.
            self.logger.trace("Refreshing chain to drop torn-down HPS tap")
            await chain.tlr_and_refresh()

        self.logger.trace("Streaming %d bits...", total_bits)
        with self.progress("config", len(blob), unit="B"):
            await self._stream_bitstream(blob, total_bits)

        self.logger.trace("Checking CONF_DONE...")

        for retry in range(15-1, -1, -1):
            await asyncio.sleep(0.1)
            try:
                cs = await sdm.config_status()
            except Error:
                if not retry:
                    raise
            if cs.conf_done:
                break

        self.logger.note("Configuration complete")
        self.BYPASS()
        await self.run(16)

        # The HPS debug TAP only joins the visible chain after a
        # JTAG Test-Logic-Reset. Drive one, re-probe, and let
        # `post_tlr` claim any new neighbour as gated-by-us.
        await chain.tlr_and_refresh()

    async def post_tlr(self):
        """Claim the freshly-attached neighbour TAP (if any) as
        gated by this Agilex.

        ``Chain.tlr_and_refresh`` creates a fresh TAP for each new
        slot it discovers and leaves the controller field empty —
        the chain layer doesn't know which controller should own
        the new entry. We do: any TAP immediately TDI-side of us
        with no controller and an ADIv5/ADIv6 ARM-DP IDCODE is the
        HPS ARM debug port the bitstream just brought online.

        Idempotent: if a TAP at the expected slot already has
        ``controller=self``, we leave it alone. If something else
        already claimed it (another driver), we also leave it
        alone — surprise ownership grabs would break the other
        driver's invariants.
        """
        chain = self.parent_of_class(Chain)
        my_ctx = chain.context(self)
        expected_ir_pre = my_ctx.ir_pre + self.irlen
        for tap in chain.children:
            if tap is self:
                continue
            ctx = chain.context(tap)
            if not ctx.enabled:
                continue
            if ctx.ir_pre != expected_ir_pre:
                continue
            if ctx.controller is not None:
                continue
            self.logger.note(
                "Claiming %s (idcode=0x%08x) as HPS-gated neighbour",
                tap.name, int(tap.idcode) if tap.idcode else 0)
            chain.tap_set_controller(tap, self)
            return

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
        """Check config status via IR 0x208."""
        tdi_val = 0
        if request_data:
            tdi_val |= 1
        if start_config:
            tdi_val |= 2
        if enable:
            tdi_val |= 4

        result = self.CONFIG_STATUS(tdi_val, read_tdo=True)
        self.run(16)

        val = int(await result)
        done = bool(val & 1)
        error = bool(val & 2)
        progress_words = (val >> 2) & 0x3FFFFFFF
        fifo_free = (val >> 32) & 0x1F
        return done, error, progress_words * 32, fifo_free

    async def _stream_bitstream(self, data, total_bits):
        """Stream bitstream data to SDM via IR 0x002 (CONFIG).

        Matches STAPL J127/J125 flow control:
        1. First poll: request_data + start_config + enable (TDI=7)
        2. Wait for done=True (SDM config engine ready)
        3. Send data chunks, poll status between each
        4. Chunk doubles on success, halves on stall
        5. Data position tracks SDM progress counter (re-sends on stall)
        """
        chunk_size = self._STREAM_INITIAL_CHUNK
        first = True       # J123: first-iteration flag
        stalled = False     # J112: was-stalled flag
        status_retries = self._STREAM_STATUS_RETRIES

        header_bs = self._STREAM_HEADER

        while True:
            # CONFIG_STATUS poll
            if first:
                request_data = True
                start_config = True
                enable = True
            else:
                request_data = stalled
                start_config = False
                enable = False

            done, error, progress, fifo_free = await self._stream_status_check(
                request_data=request_data,
                start_config=start_config,
                enable=enable,
            )

            first = False

            if error:
                raise Error(0, opcode=int(
                    Command.CONFIG_REQUEST))

            if progress >= total_bits:
                break

            status_retries -= 1
            if status_retries <= 0:
                raise Error(0, opcode=int(
                    Command.CONFIG_REQUEST))

            # Flow control
            if done:
                stalled = True
                if chunk_size > self._STREAM_INITIAL_CHUNK:
                    chunk_size //= 2
                continue

            # SDM ready for data
            if stalled:
                stalled = False
            else:
                chunk_size = min(chunk_size * 2, self._STREAM_MAX_CHUNK)

            remaining = total_bits - progress
            if remaining <= 0:
                continue

            n = min(chunk_size, remaining)

            # Data is read from SDM's progress position, not our
            # send position. On stall recovery, this re-sends data
            # the SDM hasn't consumed yet.
            data_slice = BitString(
                bytes(data[progress // 8:(progress + n + 7) // 8]), n)
            trailer = BitString(0, 1)
            frame = header_bs + data_slice + trailer

            self.CONFIG(frame, read_tdo=False)
            await self.run(16)

            status_retries = self._STREAM_STATUS_RETRIES



# ------------------------------------------------------------------
# Concrete parts
# ------------------------------------------------------------------

_AGILEX5_PARTS = {
    0x0364f0dd: "A5ED065BB32AR0",
    0x0362c0dd: "A5EA013BB23B",
}

@Tap.db.register(*map(PartId.from_idcode, _AGILEX5_PARTS.keys()))
class Agilex5E(Agilex5):
    """Agilex 5 E-series."""

    def __init__(self, idcode, **kw):
        name = _AGILEX5_PARTS.get(
            idcode & 0xfffffff, f"Agilex5-0x{idcode:08x}")
        super().__init__(idcode=idcode, name=name, **kw)
