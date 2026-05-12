from ...protocol.jtag import Tap, Dr, Instruction
from ...part_id import PartId
from ..fpga import JtagSramFpga
from ...bitstring import BitString
from .formats import RBF_SYNC, RBF_SYNC_SWAPPED


class Cyclone10(Tap, JtagSramFpga):
    """Altera/Intel Cyclone 10 LP SRAM FPGA.

    JTAG programming sequence (from SVF/openFPGALoader/STAPL analysis):
    1. IR=CONFIG (0x002), wait 12000 TCK
    2. Shift RBF data through DR (bytes are already JTAG-ordered)
    3. IR=CHECK_STATUS (0x004), read 732-bit status, verify CONF_DONE at bit 286
    4. IR=STARTUP (0x003), wait ~50000 TCK
    5. IR=BYPASS (0x3FF)
    """

    irlen = 10
    max_freq = 12e6

    # DR descriptors
    DEVICE_ID = Dr(32)
    BYPASS_REG = Dr(1)
    USER_CODE = Dr(32)
    CONFIG_STATUS = Dr(732)

    # Instruction registry — from STAPL analysis (ALG_VERSION 68)
    #
    # Core instructions:
    CONFIG = Instruction(0x002, None)            # SRAM configuration data shift
    STARTUP = Instruction(0x003, None)           # Initialize after config
    CHECK_STATUS = Instruction(0x004, "CONFIG_STATUS")  # Read config status
    IDCODE = Instruction(0x006, "DEVICE_ID")     # Read JTAG IDCODE
    USERCODE = Instruction(0x007, "USER_CODE")   # Read user code register
    BYPASS = Instruction(0x3FF, "BYPASS_REG")    # Standard JTAG bypass
    #
    # ISC (In-System Configuration) — not used for SRAM config:
    # ISC_ENABLE     = Instruction(0x071, None)  # Enter ISC mode
    # ISC_DISABLE    = Instruction(0x079, None)  # Exit ISC mode
    # PULSE_NCONFIG  = Instruction(0x281, None)  # Pulse NCONFIG (reset)
    # INTOSC_BYPASS  = Instruction(0x2EE, None)  # Internal oscillator bypass (set)
    # INTOSC_RESUME  = Instruction(0x1EE, None)  # Internal oscillator bypass (clear)
    # HALT_ON_CHIP_CC = Instruction(0x00D, None) # Debug: halt on chip CC
    # VERIFY_ENABLE  = Instruction(0x332, None)  # Enter verify mode
    # PROGRAM_SDR    = Instruction(0x044, None)  # Program shift data register

    # Status register: 732 bits total (mostly opaque configuration state).
    # Only bit 286 (CONF_DONE) is checked by STAPL/SVF.
    # Bit 285 also flips with configuration (likely nSTATUS or INIT_DONE).
    CONF_DONE_BIT = 286

    async def start(self):
        configured = await self.is_configured()
        self.logger.note("IDCODE: 0x%08x, configured: %s",
                         self.idcode, configured)
        if configured:
            usercode = int(await self.USERCODE())
            self.logger.note("UserCode: 0x%08x", usercode)

    async def load(self, source):
        blob = await source.read(0, source.size)

        # Sanity-check: data must be a parsed RBF bitstream. Common
        # mistake — pointing this at a SOF's `config_data` child,
        # which is Quartus-internal frame data, not JTAG-ready RBF.
        # Scan the first 4 KiB for either sync orientation; either is
        # fine because the Rbf parser auto-swaps to canonical order.
        head = bytes(blob[:4096])
        if RBF_SYNC not in head and RBF_SYNC_SWAPPED not in head:
            raise ValueError(
                f"{source.path}: no RBF sync word found in first 4 KiB. "
                "Cyclone 10 needs a parsed RBF bitstream — pass a .rbf "
                "file (Rbf parser handles auto-detect + bit-swap), or "
                "convert from SOF with `quartus_cpf -c file.sof file.rbf`. "
                "Passing a SOF's `config_data` child won't work — that's "
                "Quartus's frame format, not RBF.")

        # RBF bytes are already in JTAG bit order (LSB-first per byte).
        # No bit-swapping needed.
        tdi = BitString(bytes(blob), len(blob) * 8)

        self.logger.trace("Entering config mode...")
        await self.ir(int(self.CONFIG))(read_tdo=False)
        await self.run(12000)

        with self.progress("config", len(blob), unit="B"):
            self.logger.trace("Shifting %d bits of config data...", len(tdi))
            await self.ir(int(self.CONFIG), dr_length=len(tdi))(tdi, read_tdo=False)

        self.logger.trace("Checking status...")
        await self.ir(int(self.CHECK_STATUS))(read_tdo=False)
        await self.run(60)
        status = await self.CHECK_STATUS()
        conf_done = bool(int(status) & (1 << self.CONF_DONE_BIT))

        if not conf_done:
            raise RuntimeError(
                "Configuration failed: CONF_DONE not asserted "
                f"(status[{self.CONF_DONE_BIT}]=0)")

        self.logger.trace("Starting device...")
        await self.ir(int(self.STARTUP))(read_tdo=False)
        await self.run(49152)
        await self.run(512)

        await self.BYPASS()
        await self.run(12000)

        self.logger.note("Configuration complete")

    async def erase(self):
        # Cyclone 10 LP is SRAM-only: loading a new config overwrites.
        # Entering CONFIG mode implicitly erases.
        await self.ir(int(self.CONFIG))(read_tdo=False)
        await self.run(12000)

    async def is_configured(self) -> bool:
        status = await self.CHECK_STATUS()
        return bool(int(status) & (1 << self.CONF_DONE_BIT))


_PARTS = {
    0x020f30dd: "10CL025Y",
    0x020f20dd: "10CL016Y",
    0x020f10dd: "10CL006Y",
    0x020f50dd: "10CL040Y",
    0x020f40dd: "10CL025A",
    0x020f60dd: "10CL055Y",
    0x020f70dd: "10CL080Y",
    0x020f80dd: "10CL120Y",
}


@Tap.db.register(*map(PartId.from_idcode, _PARTS.keys()))
class Cyclone10Lp(Cyclone10):
    def __init__(self, idcode, **kw):
        name = _PARTS.get(idcode, f"Cyclone10-0x{idcode:08x}")
        super().__init__(idcode=idcode, name=name, **kw)
