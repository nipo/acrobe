"""iCE40 configuration RAM loading over the slave-serial port.

The FPGA is held in reset while chip select is asserted, which makes
it come up as a slave rather than booting from its own SPI flash;
the whole bitstream is then clocked in on the same wires.

The shifter is any object matching `acrobe.adapter.ftdi.spi_bitbang.
SpiBitbang`: chip select, named auxiliary outputs and named sampled
inputs, plus dummy clocks. Configuration needs to drive the FPGA's
data input, which on boards where the FPGA boots from flash is the
flash's output net — a net an MPSSE engine cannot drive.
"""

from ..fpga import SramFpga


class Ice40SlaveSerial(SramFpga):
    """iCE40 CRAM loaded through slave-serial configuration."""

    # Names the shifter must declare for the two configuration pins.
    CRESET = "creset"
    CDONE = "cdone"

    MAX_FREQ = 15e6

    # Time the configuration engine needs after reset is released
    # before it accepts the first bit.
    RESET_SETTLE_S = 1.2e-3

    # Clocks between reset release and the bitstream, with chip
    # select released.
    LEAD_CLOCKS = 8

    # Clocks given to the engine to raise CDONE once the bitstream is
    # in, and the granularity at which CDONE is sampled.
    CDONE_CLOCKS = 100
    CDONE_CHUNK = 8

    # Clocks the device needs after CDONE to release its I/Os.
    TRAILING_CLOCKS = 50

    def __init__(self, shifter, name: str = "ice40"):
        super().__init__(name)
        self.shifter = shifter

    async def load(self, source):
        blob = await source.read(0, source.size)
        self.logger.note("Loading %d bytes of configuration", len(blob))

        await self.shifter.bit_freq_cap(self.MAX_FREQ)
        await self.__reset_pulse()
        await self.shifter.cs_set(False)
        await self.shifter.clocks(self.LEAD_CLOCKS)
        await self.shifter.cs_set(True)
        await self.shifter.shift(blob)
        await self.shifter.cs_set(False)
        clocks = await self.__wait_done()
        await self.shifter.clocks(self.TRAILING_CLOCKS)
        self.logger.note("Configured, CDONE asserted after %d clocks", clocks)

    async def erase(self):
        """Clear the configuration RAM and leave the device in reset.

        Releasing reset here would restart the device's own boot from
        flash, which is the opposite of what an erase asks for."""
        await self.shifter.output_set(self.CRESET, 0)
        await self.shifter.wait(self.RESET_SETTLE_S)

    async def is_configured(self) -> bool:
        return await self.shifter.input_get(self.CDONE)

    async def __reset_pulse(self):
        """Enter slave configuration mode: chip select is asserted
        across the reset release, which is what tells the device not
        to boot on its own."""
        await self.shifter.output_set(self.CRESET, 0)
        await self.shifter.cs_set(True)
        await self.shifter.output_set(self.CRESET, 1)
        await self.shifter.wait(self.RESET_SETTLE_S)

    async def __wait_done(self) -> int:
        clocked = 0
        while clocked < self.CDONE_CLOCKS:
            count = min(self.CDONE_CHUNK, self.CDONE_CLOCKS - clocked)
            samples = await self.shifter.clocks(count, sample=self.CDONE)
            clocked += count
            if any(samples):
                return clocked
        raise RuntimeError(
            f"iCE40 CDONE still low after {clocked} clocks: "
            "configuration was rejected")
