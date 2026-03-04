import asyncio

from ...protocol.jtag import Tap, Dr, Instruction
from ..fpga import JtagSramFpga
from ...endian import bitswap8

parts = {
    0x0000: "GW2A-18/18C",
    0x0002: "GW2A-55/55C",
    0x1001: "GW1N-4",
    0x1003: "GW1N-4[BC]",
    0x1004: "GW1N-9C",
    0x1005: "GW1N-9",
    0x1006: "GW1NZ-1",
    0x1009: "GW1NS-4C",
    0x3000: "GW1NS-2",
    0x3001: "GW1NS-2C",
    0x9002: "GW1N-1",
    0x9003: "GW1N-1S",
    0x1206: "GW1-1P5/2[B]",
    0x0016: "GW5A[R]T-15",
    0x0012: "GW5A[R]-25",
    0x0014: "GW5A-60",
}

DONE_BIT = 13


def _idcode_to_part_no(idcode):
    return (idcode >> 12) & 0xffff


def _part_ids(*prefixes):
    """Return set of IDCODEs for parts whose name starts with any prefix."""
    ids = set()
    for part_no, name in parts.items():
        if any(name.startswith(p) for p in prefixes):
            ids.add((part_no << 12) | 0x81b)
    return ids


class GowinFpga(Tap, JtagSramFpga):
    irlen = 8

    USER_IR = [0x42, 0x43]

    BOUNDARY = Dr(None)
    DEVICE_ID = Dr(32)
    ISC_DEFAULT = Dr(1)
    ISC_PDATA = Dr(None)
    USERCODE_REG = Dr(32)
    STATUS_REGISTER = Dr(32)

    BYPASS2 = Instruction(0x00, "BYPASS_REG")

    ISC_DISABLE = Instruction(0x3a, "ISC_DEFAULT")
    ISC_NOOP = Instruction(0x02, "ISC_DEFAULT")
    ISC_SRAM_ERASE = Instruction(0x05, "ISC_DEFAULT")
    ISC_SRAM_ERASE_DONE = Instruction(0x09, "ISC_DEFAULT")
    ISC_ENABLE = Instruction(0x15, "ISC_DEFAULT")
    ISC_PROGRAM_DONE = Instruction(0x08, "ISC_DEFAULT")

    ISC_ADDRESS_INIT = Instruction(0x12, "ISC_DEFAULT")
    ISC_TRANSFER_CONFIG = Instruction(0x17, "ISC_PDATA")

    HIGHZ = Instruction(0x0c, "BYPASS_REG")
    CLAMP = Instruction(0x07, "BYPASS_REG")

    IDCODE = Instruction(0x11, "DEVICE_ID")
    USERCODE = Instruction(0x13, "USERCODE_REG")

    READ_STATUS = Instruction(0x41, "STATUS_REGISTER")

    PRELOAD = Instruction(0x01, "BOUNDARY")
    SAMPLE = Instruction(0x01, "BOUNDARY")
    EXTEST = Instruction(0x04, "BOUNDARY")

    USER1 = Instruction(0x42, None)
    USER2 = Instruction(0x43, None)

    def __init__(self, interface, idcode, **kw):
        part_no = _idcode_to_part_no(idcode)
        name = parts.get(part_no, f"Gowin-0x{idcode:08x}")
        super().__init__(interface, idcode, name=name, **kw)

    async def status_read(self):
        raw = await self.READ_STATUS()
        status = int(raw)
        self.logger.trace("Status: 0x%08x", status)
        return status

    def is_done(self, status):
        return bool(status & (1 << DONE_BIT))

    async def _sram_erase(self):
        self.logger.trace("Erasing SRAM")
        await self.ISC_ENABLE(read_tdo=False)
        await self.run(8)
        await self.ISC_SRAM_ERASE(read_tdo=False)
        await self.run(8)
        await self.ISC_NOOP(read_tdo=False)
        await self.run(100)
        await asyncio.sleep(10e-3)
        await self.ISC_SRAM_ERASE_DONE(read_tdo=False)
        await self.run(8)
        await self.ISC_NOOP(read_tdo=False)
        await self.run(8)
        await self.ISC_DISABLE(read_tdo=False)
        await self.run(8)
        await self.ISC_NOOP(read_tdo=False)
        await self.run(100)
        await asyncio.sleep(10e-3)

    async def sram_erase(self):
        for _ in range(3):
            await self._sram_erase()
            st = await self.status_read()
            if not self.is_done(st):
                return
        raise RuntimeError(f"SRAM erase failed, status=0x{st:08x}")

    async def _assert_done(self):
        for _ in range(3):
            await self.run(1000)
            st = await self.status_read()
            if self.is_done(st):
                return
        raise RuntimeError(f"FPGA not done after configure, status=0x{st:08x}")

    async def sram_configure(self, data):
        self.logger.trace("Loading %d bytes to SRAM", len(data))
        data = bitswap8(data)
        data = b'\xff' * 60 + data + b'\xff' * 60
        await self.ISC_ENABLE(read_tdo=False)
        await self.run(100)
        await self.ISC_ADDRESS_INIT(read_tdo=False)
        await self.run(100)
        await self.ISC_TRANSFER_CONFIG(read_tdo=False)
        await self.run(100)
        from ...bitstring import BitString
        await self.ISC_TRANSFER_CONFIG(BitString(data), read_tdo=False)
        await self.run(100)
        await self.ISC_DISABLE(read_tdo=False)
        await self.run(100)
        await self.ISC_NOOP(read_tdo=False)
        await self.run(100)
        await self._assert_done()

    async def load(self, program):
        usercode = int(await self.USERCODE())
        exp = int(program.info.get("UserCode", "0x0"), 16)
        self.logger.note("Usercode 0x%08x, expected 0x%08x", usercode, exp)
        status = await self.status_read()
        if self.is_done(status) and usercode and usercode == exp:
            self.logger.note("Usercode already matches")
            return
        await self.sram_erase()
        data = program[0].data
        await self.sram_configure(bytes(data))

    async def erase(self):
        await self.sram_erase()

    async def is_configured(self):
        st = await self.status_read()
        return self.is_done(st)


@Tap.db.register(*_part_ids("GW1"))
class Gw1n(GowinFpga):
    pass


@Tap.db.register(*_part_ids("GW2A"))
class Gw2a(GowinFpga):
    pass


@Tap.db.register(*_part_ids("GW5A"))
class Gw5a(GowinFpga):
    pass
