import pytest

from acrobe.db import NoMatch
from acrobe.memory_map import MemoryMap
from acrobe.target.arm.soc import ArmSocTarget
from acrobe.component.xilinx.boot_image import Destination
from acrobe.node import Node, Readable
from acrobe.target.arm.zynq import (
    ZynqBootLoadable, ZynqPsLoadable, zynq_probe, zynq_ps_factory,
)
from acrobe.target.debuggable import HaltCause
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


class FakeBootCore(FakeCore):
    def __init__(self, cause=HaltCause.WATCHPOINT, halts=True):
        super().__init__()
        self.cause = cause
        self.halts = halts
        self.watches = []
        self.dcache_flushed = False

    async def watchpoint_add(self, address, size, kind):
        wp = (kind, address, size)
        self.watches.append(wp)
        return wp

    async def watchpoint_remove(self, wp):
        self.watches.remove(wp)

    async def wait_halted(self, timeout):
        return self.halts

    async def halt_cause(self):
        return self.cause

    async def reg_read(self, regs):
        return {"pc": 0x1234}

    def lookup_register(self, name):
        return name

    async def dcache_clean_invalidate(self):
        self.dcache_flushed = True

    async def mem_read(self, address, size):
        return bytes(size)

    async def run_at(self, address):
        self.ran_at = address
        self.watching_at_run = list(self.watches)


class FakeBus:
    def __init__(self):
        self.writes = []

    async def mem_write(self, address, data):
        self.writes.append((address, bytes(data)))

    async def mem_read(self, address, size):
        return bytes(size)


class FakePl:
    def __init__(self):
        self.loaded = None

    async def load(self, bitstream):
        self.loaded = bitstream


class FakePart(Node, Readable):
    def __init__(self, node_name, data=b"", **metadata):
        super().__init__(node_name)
        self.data = data
        self.metadata.update(metadata)

    @property
    def size(self):
        return len(self.data)

    async def read(self, offset, size):
        return self.data[offset:offset + size]


def boot_source(*partitions, register_init=()):
    source = Node("boot.bin")
    source.metadata["register_init"] = list(register_init)
    source.child_add(FakePart("fsbl", b"FSBL", load_address=0, entry=0,
                              destination=Destination.PS))
    table = Node("partition")
    table.child_add(FakePart("0", b"FSBL", name="fsbl.elf", load_address=0,
                             entry=0, destination=Destination.PS))
    for p in partitions:
        table.child_add(p)
    source.child_add(table)
    return source


def boot_loadable(cpu0, pl=None):
    target = Node("zynq")
    ahb = FakeBus()
    boot = ZynqBootLoadable(ZynqPsLoadable(cpu0, None), ahb, pl)
    target.child_add(boot)
    return boot, ahb


def app(load=0x4000000, entry=0x4000000):
    return FakePart("1", b"APP!", name="u-boot.bin", load_address=load,
                    entry=entry, destination=Destination.PS)


def bitstream():
    pl = Node("2")
    pl.metadata.update(name="top.bit", load_address=0, entry=0,
                       destination=Destination.PL)
    pl.child_add(FakePart("bitstream", b"\xaa\x99\x55\x66"))
    return pl


async def test_boot_runs_fsbl_then_places_partitions():
    cpu0, pl = FakeBootCore(), FakePl()
    boot, ahb = boot_loadable(cpu0, pl)
    source = boot_source(bitstream(), app(),
                         register_init=[(0xf8000100, 5)])
    await boot.write(source, do_start=True)
    assert cpu0.memory[0xf8000100] == (5).to_bytes(4, "little")
    assert cpu0.memory[0] == b"FSBL"
    assert cpu0.watching_at_run == [] and cpu0.ran_at == 0x4000000
    assert cpu0.watches == []
    assert cpu0.dcache_flushed and cpu0.caches_off
    assert cpu0.memory[ZynqBootLoadable.SWDT_MODE] == \
        ZynqBootLoadable.SWDT_DISABLE.to_bytes(4, "little")
    assert pl.loaded is source.child_lookup("partition").children[1] \
        .child_lookup("bitstream")
    assert (0x4000000, b"APP!") in ahb.writes
    assert (ZynqBootLoadable.LVL_SHFTR_EN,
            ZynqBootLoadable.LVL_SHFTR_ALL.to_bytes(4, "little")) in ahb.writes


async def test_boot_stops_fsbl_on_boot_mode_read():
    cpu0 = FakeBootCore()
    runs = []
    run_at = cpu0.run_at

    async def record(address):
        await run_at(address)
        runs.append((address, cpu0.watching_at_run))
    cpu0.run_at = record
    boot, _ = boot_loadable(cpu0)
    await boot.write(boot_source(app()), do_start=False)
    assert runs == [(0, [(3, ZynqBootLoadable.BOOT_MODE, 4)])]


async def test_boot_refuses_partition_below_ddr():
    cpu0 = FakeBootCore()
    boot, _ = boot_loadable(cpu0)
    with pytest.raises(ValueError, match="below DDR"):
        await boot.write(boot_source(app(load=0)))
    assert cpu0.ran_at is None


async def test_boot_refuses_pl_without_pl():
    boot, _ = boot_loadable(FakeBootCore())
    with pytest.raises(ValueError, match="no PL"):
        await boot.write(boot_source(bitstream()))


async def test_boot_fsbl_timeout_reports_pc():
    cpu0 = FakeBootCore(halts=False)
    boot, _ = boot_loadable(cpu0)
    with pytest.raises(TimeoutError, match="0x00001234"):
        await boot.write(boot_source(app()))
    assert cpu0.watches == []


async def test_boot_fsbl_unexpected_halt():
    boot, _ = boot_loadable(FakeBootCore(cause=HaltCause.BREAKPOINT))
    with pytest.raises(RuntimeError):
        await boot.write(boot_source(app()))
