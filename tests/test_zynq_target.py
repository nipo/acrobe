import pytest

from acrobe.db import NoMatch
from acrobe.memory_map import MemoryMap
from acrobe.target.arm.soc import ArmSocTarget
from acrobe.target.arm.zynq import (
    ZynqPsLoadable, zynq_probe, zynq_ps_factory,
)
from acrobe.part_id import PartId


class FakeCore:
    def __init__(self):
        self.memory = {}
        self.ran_at = None
        self.halted = False
        self.caches_off = False

    async def halt(self):
        self.halted = True

    async def caches_disable(self):
        self.caches_off = True

    async def mem_write(self, address, data):
        self.memory[address] = bytes(data)

    async def run_at(self, address):
        self.ran_at = address


def image(address, size, entry):
    m = MemoryMap([(address, bytes(size))])
    m.entry = entry
    return m


async def test_load_parks_cpu1_and_starts_cpu0():
    cpu0, cpu1 = FakeCore(), FakeCore()
    loadable = ZynqPsLoadable(cpu0, cpu1)
    await loadable.pre_program(do_erase=False, assume_clean=False)
    assert cpu0.halted and cpu0.caches_off and cpu1.caches_off
    assert cpu0.memory[ZynqPsLoadable.L2_CONTROL] == bytes(4)
    await loadable.post_program(success=True, do_start=True,
                                memory_map=image(0x10000, 16, 0x10000))
    assert cpu0.ran_at == 0x10000
    assert cpu1.ran_at == ZynqPsLoadable.CPU1_LOOP
    loop = cpu0.memory[ZynqPsLoadable.CPU1_LOOP]
    assert len(loop) == 4 * len(ZynqPsLoadable.CPU1_LOOP_CODE)
    assert cpu0.memory[ZynqPsLoadable.CPU1_START] \
        == ZynqPsLoadable.CPU1_LOOP.to_bytes(4, "little")


async def test_image_over_wait_loop_leaves_cpu1_halted():
    cpu0, cpu1 = FakeCore(), FakeCore()
    loadable = ZynqPsLoadable(cpu0, cpu1)
    await loadable.post_program(success=True, do_start=True,
                                memory_map=image(0xffff0000, 0x10000, 0xffff0000))
    assert cpu0.ran_at == 0xffff0000
    assert cpu1.ran_at is None


async def test_no_start_leaves_cores():
    cpu0, cpu1 = FakeCore(), FakeCore()
    loadable = ZynqPsLoadable(cpu0, cpu1)
    await loadable.post_program(success=True, do_start=False,
                                memory_map=image(0x10000, 16, 0x10000))
    assert cpu0.ran_at is None and cpu1.ran_at is None


def test_ps_registered_by_rom_table_part():
    assert zynq_ps_factory in ArmSocTarget.db.get(
        PartId.from_idcode(0x203b2313))


def test_probe_needs_dap_neighbour():
    class Chain:
        children = []

    class Pl:
        name = "Zynq"
        parent = Chain()

    pl = Pl()
    Chain.children = [pl]
    with pytest.raises(NoMatch):
        zynq_probe(pl)
