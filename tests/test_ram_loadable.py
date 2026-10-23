"""RamLoadable: halt before load, start from the image entry."""

import pytest

from acrobe.memory_map import MemoryMap
from acrobe.target import RamLoadable, Ram, Target
from acrobe.target.debuggable import Core


class FakeCore(Core):
    def __init__(self):
        super().__init__("core")
        self.log = []

    async def halt(self):
        self.log.append("halt")

    async def run_at(self, address):
        self.log.append(("run_at", address))


class MemRam(Ram):
    def __init__(self, name, address, size, log):
        super().__init__(name, address, size)
        self.data = bytearray(size)
        self.log = log

    async def read(self, offset, size):
        return bytes(self.data[offset:offset + size])

    async def write(self, offset, data):
        self.log.append(("write", self.address + offset))
        self.data[offset:offset + len(data)] = data


def build():
    core = FakeCore()
    loadable = RamLoadable(core)
    Target("t").child_add(loadable)
    loadable.child_add(MemRam("ram", 0x4000, 0x100, core.log))
    return core, loadable


def image(entry):
    m = MemoryMap([(0x4010, b"\x01\x02\x03\x04")])
    m.entry = entry
    return m


async def test_load_and_start():
    core, loadable = build()
    await loadable.write(image(0x4010), do_verify=True, do_start=True)
    assert core.log == ["halt", ("write", 0x4010), ("run_at", 0x4010)]


async def test_load_without_start():
    core, loadable = build()
    await loadable.write(image(0x4010))
    assert core.log == ["halt", ("write", 0x4010)]


async def test_start_requires_entry():
    core, loadable = build()
    with pytest.raises(ValueError):
        await loadable.write(image(None), do_start=True)
