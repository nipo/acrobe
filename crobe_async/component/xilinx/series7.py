import struct

from ...protocol.jtag import Tap, Dr, Instruction
from ..fpga import JtagSramFpga
from ...endian import swib_u32
from .config_access_port import ConfigAccessPort


class Series7(Tap, JtagSramFpga, ConfigAccessPort):
    irlen = 6

    USER_IR = [0x02, 0x03, 0x22, 0x23]

    # Instruction registry
    ISC_DEFAULT = Dr(1)
    CONFIG = Dr(None)
    DEVICE_ID = Dr(32)

    BYPASS = Instruction(-1, "BYPASS_REG")
    IDCODE = Instruction(0x09, "DEVICE_ID")
    IR_ISC_NOP = Instruction(0x14, None)
    IR_JPROGRAM = Instruction(0x0b, "ISC_DEFAULT")
    IR_JSTART = Instruction(0x0c, None)
    IR_CFG_IN = Instruction(0x05, "CONFIG")
    IR_CFG_OUT = Instruction(0x04, "CONFIG")

    # Config port parameters
    CFG_PREFIX = [0xaa995566]
    CFG_CMD = 0x04
    IR_STATUS_BITS = {
        'isc_done': 2,
        'isc_enabled': 3,
        'init': 4,
        'done': 5,
    }

    @staticmethod
    def type1(op, addr, count):
        return [(1 << 29) | (op << 27) | (addr << 13) | count]

    @staticmethod
    def _cfg_conv_tdi(words):
        return struct.pack("<%dL" % len(words), *map(swib_u32, words))

    @staticmethod
    def _cfg_conv_tdo(data):
        return [swib_u32(x) for x in struct.unpack("<%dL" % (len(data) // 4), data)]

    async def load(self, program):
        if len(program) != 1:
            raise ValueError("Bitstream programming only supports one config payload")

        blob = program[0].data
        if len(blob) & 3:
            raise ValueError("Bitstream data length not 32-bit aligned")

        prog_data = struct.unpack(">" + "L" * (len(blob) // 4), blob)

        self.logger.trace("Resetting...")
        await self.ir(int(self.IR_JPROGRAM))(read_tdo=False)
        await self.run(20)
        await self.ir(int(self.IR_ISC_NOP))(read_tdo=False)
        await self.run(20)

        self.logger.trace("Loading %d config words...", len(prog_data))
        await self._cfg_shift(int(self.IR_CFG_IN), prog_data)
        await self.run(100000)

        self.logger.trace("Starting...")
        await self.ir(int(self.IR_JSTART))(read_tdo=False)
        await self.run(10000)
        await self.BYPASS()
        await self.run(10000)

        if not await self.send_op_wait(-1, done=True):
            raise RuntimeError("Unable to start FPGA")

    async def erase(self):
        await self.ir(int(self.IR_JPROGRAM))(read_tdo=False)
        await self.ir(int(self.IR_ISC_NOP))(read_tdo=False)
        await self.run(20)

    async def is_configured(self) -> bool:
        status = await self.ir_status()
        return bool(int(status) & (1 << self.IR_STATUS_BITS['done']))
