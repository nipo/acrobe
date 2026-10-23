"""ARM926EJ-S CP15 memory system control."""

from acrobe.component.arm.arm9.arm926 import Arm926Cp15


class FakeCp15Debug:
    def __init__(self, control):
        self.cp15 = {(0, 1, 0, 0): control, (0, 15, 0, 0): 0x2000}
        self.log = []

    async def cp15_read(self, opc1, crn, crm, opc2):
        self.log.append(("r", crn, crm, opc2))
        if (opc1, crn, crm, opc2) == (0, 7, 14, 3):
            assert self.cp15[(0, 15, 0, 0)] & (1 << 19)
            return 1 << 30
        return self.cp15[(opc1, crn, crm, opc2)]

    async def cp15_write(self, opc1, crn, crm, opc2, value):
        self.log.append(("w", crn, crm, opc2, value))
        self.cp15[(opc1, crn, crm, opc2)] = value


async def test_caches_disable():
    debug = FakeCp15Debug(0x00051007)
    await Arm926Cp15(debug).caches_disable()
    assert debug.cp15[(0, 1, 0, 0)] == 0x00050002
    assert debug.cp15[(0, 15, 0, 0)] == 0x2000
    assert ("r", 7, 14, 3) in debug.log
    assert ("w", 7, 10, 4, 0) in debug.log
    assert ("w", 7, 5, 0, 0) in debug.log


async def test_caches_disable_skips_clean_when_dcache_off():
    debug = FakeCp15Debug(0x00051001)
    await Arm926Cp15(debug).caches_disable()
    assert ("r", 7, 14, 3) not in debug.log
    assert debug.cp15[(0, 1, 0, 0)] == 0x00050000


async def test_tcm_enable_keeps_size():
    debug = FakeCp15Debug(0)
    debug.cp15[(0, 9, 1, 1)] = 0x14
    cp15 = Arm926Cp15(debug)
    await cp15.tcm_enable(True, 0)
    assert debug.cp15[(0, 9, 1, 1)] == 0x15
    debug.cp15[(0, 9, 1, 0)] = 0x10
    await cp15.tcm_enable(False, 0x10000000)
    assert debug.cp15[(0, 9, 1, 0)] == 0x10000011
    region = await cp15.tcm_region(False)
    assert region.size_bytes == 8192
