"""EZ-USB FX3 target model."""

from acrobe.component.arm.arm9.debug import Arm9Debug
from acrobe.component.cypress.fx3 import Fx3Tap
from acrobe.memory_map import MemoryMap
from acrobe.part_id import PartId
from acrobe.target import Memory, Target
from acrobe.target.arm.arm9 import Arm9Debuggable
from acrobe.target.arm.fx3 import Fx3Loadable, Fx3Target


def fx3_tap():
    tap = Fx3Tap(idcode=int(PartId(0, 0x34, 0x7926, 1)), irlen=4)
    tap.child_add(Arm9Debug())
    return tap


def test_registered_on_tap():
    assert any(Fx3Tap in e.component_types and e.func is Fx3Target
               for e in Target.explorers)


def test_views():
    target = Fx3Target(fx3_tap())
    loadable = target.children_of_class(Fx3Loadable)[0]
    assert [(r.name, r.address, r.size) for r in sorted(loadable.regions)] == [
        ("itcm", 0, 0x4000),
        ("dtcm", 0x10000000, 0x2000),
        ("sysmem", 0x40000000, 0x80000)]
    memory = target.children_of_class(Memory)[0]
    assert {r.name for r in memory.children} >= {"mmio", "bootrom", "vic"}
    debug = target.children_of_class(Arm9Debuggable)[0]
    assert debug.cores == [target.core]


class FakeCp15:
    def __init__(self, log):
        self.log = log

    async def tcm_enable(self, instruction, address):
        self.log.append(("tcm", instruction, address))


class FakeCore:
    def __init__(self):
        self.log = []
        self.cp15 = FakeCp15(self.log)
        self.memory = {}

    async def halt(self):
        self.log.append("halt")

    async def caches_disable(self):
        self.log.append("caches_disable")

    async def mem_write(self, address, data):
        self.log.append(("write", address, len(data)))

    async def run_at(self, address):
        self.log.append(("run_at", address))


async def test_load_sequence():
    target = Fx3Target(fx3_tap())
    loadable = target.children_of_class(Fx3Loadable)[0]
    core = FakeCore()
    loadable.core = core
    for region in loadable.regions:
        region.bus = core
    image = MemoryMap([(0x00000000, b"\0" * 0x100),
                       (0x40003000, b"\1" * 0x40)])
    image.entry = 0x40003000
    await loadable.write(image, do_start=True)
    assert core.log == [
        "halt", "caches_disable",
        ("tcm", True, 0), ("tcm", False, 0x10000000),
        ("write", 0, 0x100), ("write", 0x40003000, 0x40),
        ("run_at", 0x40003000)]
