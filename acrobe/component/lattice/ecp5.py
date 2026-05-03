import asyncio

from ...protocol.jtag import Tap, Dr, Instruction
from ...part_id import PartId
from ..fpga import JtagSramFpga
from ...bitstring import BitString
from ...endian import bitswap8
from ...bitfield import Bitfield, BooleanField, MappingField, Field


_ECP5_PARTS = {
    0x21111043: "LFE5U-12",
    0x41111043: "LFE5U-25",
    0x41112043: "LFE5U-45",
    0x41113043: "LFE5U-85",
    0x01111043: "LFE5UM-25",
    0x01112043: "LFE5UM-45",
    0x01113043: "LFE5UM-85",
    0x81111043: "LFE5UM5G-25",
    0x81112043: "LFE5UM5G-45",
    0x81113043: "LFE5UM5G-85",
}


class ECP5Status(Bitfield):
    TransparentMode = BooleanField(0)
    ConfigTarget    = Field(1, 3)
    JTAG            = BooleanField(4)
    Done            = BooleanField(8)
    ISC             = BooleanField(9)
    Write           = BooleanField(10)
    Read            = BooleanField(11)
    Busy            = BooleanField(12)
    Fail            = BooleanField(13)
    StdPreamble     = BooleanField(21)
    SPIFail         = BooleanField(22)
    BSEError        = MappingField(23, 3,
                        ["OK", "ID", "CMD", "CRC", "Preamble",
                         "Abort", "Overflow", "SDM"])
    ExecutionError  = BooleanField(26)


@Tap.db.register(*map(PartId.from_idcode, _ECP5_PARTS.keys()))
class ECP5(Tap, JtagSramFpga):
    irlen = 8
    max_freq = 25e6

    USER_IR = [0x32, 0x38]

    # Data registers
    STATUS      = Dr(32)
    ISC_CONFIG  = Dr(8)
    ISC_DEFAULT = Dr(1)
    ISC_SECTOR  = Dr(8)
    BUSY        = Dr(1)
    BITSTREAM   = Dr(None)
    DEVICE_ID   = Dr(32)
    PRELOAD_REG = Dr(208)

    # Instructions
    ISC_ENABLE          = Instruction(0xc6, "ISC_CONFIG")
    ISC_DISABLE         = Instruction(0x26, "ISC_DEFAULT")
    ISC_ERASE           = Instruction(0x0e, "ISC_SECTOR")
    LSC_READ_STATUS     = Instruction(0x3c, "STATUS")
    LSC_CHECK_BUSY      = Instruction(0xf0, "BUSY")
    LSC_INIT_ADDRESS    = Instruction(0x46, "ISC_DEFAULT")
    LSC_BITSTREAM_BURST = Instruction(0x7a, "BITSTREAM")
    LSC_REFRESH         = Instruction(0x79, "ISC_DEFAULT")
    IDCODE              = Instruction(0xe0, "DEVICE_ID")
    USERCODE            = Instruction(0xc0, "DEVICE_ID")
    PRELOAD_SAMPLE      = Instruction(0x1c, "PRELOAD_REG")

    ERASE_SRAM = 1

    def __init__(self, idcode, **kw):
        name = _ECP5_PARTS.get(idcode & 0x0FFFFFFF, f"ECP5-0x{idcode:08x}")
        super().__init__(idcode=idcode, name=name, **kw)

    async def _read_status(self):
        raw = await self.LSC_READ_STATUS()
        status = ECP5Status(int(raw))
        self.logger.trace("Status: %s", status)
        return status

    async def _wait_not_busy(self, timeout=1.0, step=0.01):
        for _ in range(max(int(timeout / step), 1)):
            raw = await self.LSC_CHECK_BUSY()
            if not (int(raw) & 1):
                return
            await asyncio.sleep(step)
        raise RuntimeError("ECP5 busy flag stuck")

    async def _isc_enable(self, target=0x00):
        await self.ISC_ENABLE(target, read_tdo=False)
        await self.run(1000)
        await self._wait_not_busy()
        status = await self._read_status()
        if not status.ISC:
            raise RuntimeError(f"ECP5 ISC enable failed (ISC not set): {status}")
        if status.Fail:
            raise RuntimeError(f"ECP5 ISC enable failed: {status}")

    async def _isc_disable(self):
        await self.ISC_DISABLE(read_tdo=False)
        await self.run(1000)
        await self._wait_not_busy()

    async def start(self):
        status = await self._read_status()
        self.logger.note("IDCODE: 0x%08x, %s", self.idcode, status)

    async def _assert_done(self):
        for _ in range(3):
            await self.run(1000)
            status = await self._read_status()
            if status.Done:
                return
        raise RuntimeError(f"ECP5 SRAM load failed (DONE not set): {status}")

    async def load(self, source):
        blob = await source.read(0, source.size)
        # Bitswap: bitstream file is MSB-first (SPI order),
        # JTAG shifts LSB-first, so reverse bits in each byte.
        blob = bitswap8(bytes(blob))

        # Preload boundary scan with all-ones to clear I/O state
        await self.PRELOAD_SAMPLE(BitString(b'\xff' * 26), read_tdo=False)

        # Refresh to clear stale configuration state
        # (e.g. SPIFail/BSEError from failed SPI boot)
        await self.LSC_REFRESH(read_tdo=False)
        await self.run(1000)
        await asyncio.sleep(0.1)

        await self._isc_enable(target=0x00)

        # Erase SRAM before loading (required by TN02039)
        await self.ISC_ERASE(self.ERASE_SRAM, read_tdo=False)
        await self.run(1000)
        await self._wait_not_busy(timeout=5.0)
        status = await self._read_status()
        if status.Fail:
            raise RuntimeError(f"ECP5 SRAM erase failed: {status}")

        # Reset configuration address
        self.LSC_INIT_ADDRESS(read_tdo=False)
        await self.run(1000)

        # Burst bitstream data
        self.LSC_BITSTREAM_BURST(BitString(blob), read_tdo=False)
        await self.run(1000)

        # Check DONE before leaving ISC mode
        await self._assert_done()

        await self.BYPASS(read_tdo=False)
        await self._isc_disable()
        await self.BYPASS(read_tdo=False)

        self.logger.note("SRAM load complete")

    async def erase(self):
        await self._isc_enable(target=0x00)
        await self.ISC_ERASE(self.ERASE_SRAM, read_tdo=False)
        await self.run(100)
        await self._wait_not_busy(timeout=5.0)
        await self._isc_disable()

    async def is_configured(self):
        status = await self._read_status()
        return status.Done
