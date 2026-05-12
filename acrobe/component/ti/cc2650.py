"""TI CC2650 ICE-Pick handler.

Plugs three chip-specific secondary TAPs (DFT, Efuse, AON) and the
ARM JTAG-DP behind a CC2650 ICE-Pick router. The base
:class:`IcePick` enumerates every sub-TAP in ``TAPS`` whose router
state reports ``tap_present`` and ``tap_accessible`` and enables
them; the cool-down loop detaches the ones nothing actually uses,
so the scan chain stays short while every sub-TAP remains one
auto-wake away from being available again.
"""

from ...part_id import PartId
from ...protocol import jtag
from ..arm.jtag_dp import JtagDpTap
from .icepick import Block, IcePick, IcePickBlock, Router


class Cc2650DftTap(jtag.Tap):
    """CC2650 Design-For-Test tap. Test Tap #0.

    Known IR map (from crobe field probing):
        IR 0x00 : 1-bit
        IR 0x06 : 78-bit PROFILE
        IR 0x07 : 20-bit
        IR 0x08 : 32-bit
        IR 0x09 : 28-bit
        IR 0x0b : 12-bit
        IR 0x0f : 1-bit (BYPASS)
    """

    irlen = 4

    PROFILE_REG = jtag.Dr(78)
    PROFILE = jtag.Instruction(6, "PROFILE_REG")

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "TI CC2650 DFT"
        super().__init__(idcode=idcode, irlen=irlen, name=name)


class Cc2650EfuseTap(jtag.Tap):
    """CC2650 e-fuse tap. Test Tap #3.

    Only IR 0x09 carries a non-BYPASS register (40 bits); the rest
    bypass at 1 bit each.
    """

    irlen = 4

    EFUSE_REG = jtag.Dr(40)
    EFUSE = jtag.Instruction(9, "EFUSE_REG")

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "TI CC2650 Efuse"
        super().__init__(idcode=idcode, irlen=irlen, name=name)


class Cc2650AonTap(jtag.Tap):
    """CC2650 Always-On tap. Test Tap #5.

    The CC2650 routes its AON/PRCM control through this tap. Only the
    7-bit register at IR 0x0c is exercised by current code; the rest
    of the IR space is mapped above for documentation.
    """

    irlen = 4

    AON_CTRL_REG = jtag.Dr(7)
    AON_CTRL = jtag.Instruction(0xc, "AON_CTRL_REG")

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "TI CC2650 AON"
        super().__init__(idcode=idcode, irlen=irlen, name=name)


@jtag.Tap.db.register(PartId.from_idcode(0x8b99a02f))
class Cc2650Icepick(IcePick):
    """ICE-Pick instance for the TI CC2650 wireless MCU.

    ``TAPS`` is the full manifest of sub-TAPs the chip exposes. The
    base ``IcePick.start`` cross-references it against the router's
    accessibility bits and enables everything that's reachable.
    """

    TAPS = {
        (Block.DebugTap, 0): (0x4ba00477, JtagDpTap),
        (Block.TestTap, 0): (None, Cc2650DftTap),
        (Block.TestTap, 3): (None, Cc2650EfuseTap),
        (Block.TestTap, 5): (None, Cc2650AonTap),
    }

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "TI CC2650 ICE-Pick"
        super().__init__(idcode=idcode, irlen=irlen, name=name)

    async def system_reset(self):
        """Pulse the IcePick's system reset line.

        Writes 0x41 then 0x00 into the IcePick Control register; the
        chip latches a system-wide reset on the rising edge of the
        write.
        """
        await self.router_write(Block.IcePick, IcePickBlock.Control, 0x41)
        await self.router_write(Block.IcePick, IcePickBlock.Control, 0x00)
