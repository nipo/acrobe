import struct

from ...protocol.jtag import Tap, Dr, Instruction
from ..fpga import JtagSramFpga
from ...endian import swib_u16
from .config_access_port import ConfigAccessPort


class Series6(Tap, JtagSramFpga, ConfigAccessPort):
    irlen = 6

    USER_IR = [0x02, 0x03, 0x1a, 0x1b]

    # Instruction registry
    ISC_DEFAULT = Dr(1)
    CONFIG = Dr(None)
    DEVICE_ID = Dr(32)
    USER_CODE = Dr(32)

    BYPASS = Instruction(-1, "BYPASS_REG")
    IDCODE = Instruction(0x09, "DEVICE_ID")
    IR_USERCODE = Instruction(0x08, "USER_CODE")
    IR_ISC_NOP = Instruction(0x14, "ISC_DEFAULT")
    IR_JPROGRAM = Instruction(0x0b, "ISC_DEFAULT")
    IR_JSTART = Instruction(0x0c, "ISC_DEFAULT")
    IR_CFG_IN = Instruction(0x05, "CONFIG")
    IR_CFG_OUT = Instruction(0x04, "CONFIG")

    # Config port parameters
    CFG_PREFIX = [0xaa99, 0x5566]
    CFG_CMD = 0x05
    IR_STATUS_BITS = {
        'isc_done': 2,
        'isc_enabled': 3,
        'init': 4,
        'done': 5,
    }

    @staticmethod
    def type1(op, addr, count):
        return [(1 << 13) | (op << 11) | (addr << 5) | count]

    @staticmethod
    def _cfg_conv_tdi(words):
        return struct.pack("<%dH" % len(words), *map(swib_u16, words))

    @staticmethod
    def _cfg_conv_tdo(data):
        return [swib_u16(x) for x in struct.unpack("<%dH" % (len(data) // 2), data)]

    async def start(self):
        configured = await self.is_configured()
        status = await self.ir_status()
        self.logger.note("IDCODE: 0x%08x, ir_status: 0x%04x, configured: %s",
                         self.idcode, int(status), configured)
        if configured:
            userid = int(await self.IR_USERCODE())
            self.logger.note("UserID: 0x%08x", userid)

    async def load(self, program):
        if len(program) != 1:
            raise ValueError("Bitstream programming only supports one config payload")

        blob = program[0].data
        if len(blob) % 2:
            raise ValueError("Odd data length in bitstream")

        userid = program.info.get("userid")
        if userid is not None and userid != 0xffffffff:
            hw_userid = int(await self.IR_USERCODE())
            if hw_userid == userid:
                self.logger.trace("UserID match (0x%08x), skipping reload", userid)
                if await self.send_op_wait(-1, done=True):
                    return

        prog_data = struct.unpack(">" + "H" * (len(blob) // 2), blob)

        self.logger.trace("Resetting...")
        if not await self.send_op_wait(int(self.IR_JPROGRAM), init=True):
            raise RuntimeError("Unable to reset FPGA")

        self.logger.trace("Loading %d config words...", len(prog_data))
        await self._cfg_shift(int(self.IR_CFG_IN), prog_data)
        await self.run(40)

        self.logger.trace("Starting...")
        if not await self.send_op_wait(int(self.IR_JSTART), done=True):
            raise RuntimeError("Unable to start FPGA")

    async def erase(self):
        await self.ir(int(self.IR_JPROGRAM))(read_tdo=False)
        await self.ir(int(self.IR_ISC_NOP))(read_tdo=False)
        await self.run(20)

    async def is_configured(self) -> bool:
        status = await self.ir_status()
        return bool(int(status) & (1 << self.IR_STATUS_BITS['done']))
