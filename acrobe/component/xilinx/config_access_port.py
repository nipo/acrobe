import struct

from ...bitstring import BitString


class ConfigAccessPort:
    """Mixin for Xilinx JTAG config port access.

    Must be mixed into a Tap subclass. Class attributes to set:

        CFG_PREFIX: list       # sync header words
        CFG_CMD: int           # command register address
        IR_STATUS_BITS: dict   # {'done': 5, 'init': 4, ...}
        cfg_struct: str        # struct format char ('H' for 16-bit, 'L' for 32-bit)
        cfg_word_size: int     # bytes per config word (2 or 4)
        IR_CFG_IN: int         # IR value for CFG_IN instruction
        IR_CFG_OUT: int        # IR value for CFG_OUT instruction

    Must implement:
        type1(op, addr, count) -> list of config words  (staticmethod)
        _cfg_conv_tdi(words) -> bytes                   (staticmethod)
        _cfg_conv_tdo(data) -> list of words            (staticmethod)
    """

    OP_NOP = 0
    OP_READ = 1
    OP_WRITE = 2

    async def cfg_read(self, reg, count):
        """Read config register(s) via JTAG config port."""
        nop = self.type1(self.OP_NOP, 0, 0)
        cmd = self.type1(self.OP_READ, reg, count)

        await self._cfg_shift(self.IR_CFG_IN, self.CFG_PREFIX + nop + cmd + nop + nop)
        ret = await self._cfg_shift(self.IR_CFG_OUT, [0] * count, read_rsp=True)
        await self.BYPASS()
        return ret

    async def cfg_cmd(self, cmd):
        """Write a command to the config command register."""
        nop = self.type1(self.OP_NOP, 0, 0)
        cmd_write = self.type1(self.OP_WRITE, self.CFG_CMD, 1)

        await self._cfg_shift(self.IR_CFG_IN, self.CFG_PREFIX + nop + cmd_write + [cmd] + nop)
        await self.BYPASS()

    async def _cfg_shift(self, ir_instr, words, read_rsp=False):
        """Shift config words through the config data register.

        ir_instr: IR value (int) or TapInstruction for the config register.
        words: list of config words.
        read_rsp: if True, read TDO and convert back to words.
        """
        blob = self._cfg_conv_tdi(words)
        tdi = BitString(blob, len(blob) * 8)

        result = await self.ir(ir_instr)(tdi, read_tdo=read_rsp)
        await self.run(30)

        if read_rsp:
            return self._cfg_conv_tdo(bytes(result))

    async def send_op_wait(self, ir_value, **expected):
        """Shift an IR value and poll ir_status until expected bits match.

        ir_value: IR value to shift (int), or -1 for just polling.
        expected: keyword args mapping bit names to expected bool values,
                  e.g. done=True, init=True.
        """
        if ir_value >= 0:
            await self.ir(ir_value)(read_tdo=False)

        for _ in range(50):
            await self.run(40)
            status = await self.ir_status()
            status_int = int(status)

            ok = all(
                bool(status_int & (1 << self.IR_STATUS_BITS[k])) == v
                for k, v in expected.items()
            )
            if ok:
                return True

        return False
