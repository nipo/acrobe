"""Tests for the RP2040 PICOBOOT-backed Target probe.

Exercises the probe shape end-to-end against a mock Picoboot
transport — verifies the produced Target carries the Loadable,
Flash region, and Puppet that downstream CLI commands rely on,
and that programming flow routes through the transport's
flash_erase / write commands.
"""

import asyncio
import struct

import pytest

from acrobe.component.raspberry.picoboot import Picoboot, PicobootPuppet
from acrobe.target import Target
from acrobe.target.arm.rp2040 import (
    Rp2040Target, PicobootXipFlash, PicobootLoadable,
    XIP_BASE, DEFAULT_FLASH_SIZE,
)


class MockPicobootTransport:
    def __init__(self):
        self.flash = bytearray(b"\xff" * 0x200000)
        self.read_log: list[tuple[int, int]] = []
        self.write_log: list[tuple[int, bytes]] = []
        self.erase_log: list[tuple[int, int]] = []
        self.exclusive_log: list[int] = []
        self.reboots: list[tuple[int, int, int]] = []

    def __offset(self, addr):
        return addr - XIP_BASE

    async def read(self, addr, size):
        self.read_log.append((addr, size))
        off = self.__offset(addr)
        return bytes(self.flash[off:off + size])

    async def write(self, addr, data):
        data = bytes(data)
        self.write_log.append((addr, data))
        off = self.__offset(addr)
        self.flash[off:off + len(data)] = data

    async def flash_erase(self, addr, size):
        self.erase_log.append((addr, size))
        off = self.__offset(addr)
        for p in range(off, off + size):
            self.flash[p] = 0xFF

    async def exec(self, pc):
        raise AssertionError("XIP flash path should never call exec")

    async def exclusive_access(self, mode):
        self.exclusive_log.append(mode)

    async def reboot(self, pc=0, sp=0, delay_ms=100):
        self.reboots.append((pc, sp, delay_ms))


async def build_target():
    """Drive the probe directly with a synthetic Picoboot Node."""
    transport = MockPicobootTransport()
    picoboot = Picoboot(transport)
    # Run the probe under whatever explorer Target uses internally.
    # We can't easily replay TargetDiscovery against a fake parent
    # without setting up an HwRoot, so call the registered probe
    # function directly via Target.explorers_for or the registry.
    # Simpler: import the function and call it.
    from acrobe.target.arm.rp2040 import rp2040_picoboot_probe
    target = await rp2040_picoboot_probe(picoboot)
    return target, picoboot, transport


class TestProbeShape:
    @pytest.mark.asyncio
    async def test_returns_rp2040_target(self):
        target, _, _ = await build_target()
        assert isinstance(target, Rp2040Target)

    @pytest.mark.asyncio
    async def test_has_loadable_with_flash(self):
        target, _, _ = await build_target()
        from acrobe.target import Loadable
        loadables = target.children_of_class(Loadable)
        assert len(loadables) == 1
        loadable = loadables[0]
        assert isinstance(loadable, PicobootLoadable)
        flashes = loadable.children_of_class(PicobootXipFlash)
        assert len(flashes) == 1
        f = flashes[0]
        assert f.address == XIP_BASE
        assert f.size == DEFAULT_FLASH_SIZE
        assert f.write_page_size == 256
        assert f.erase_page_sizes == [4096]

    @pytest.mark.asyncio
    async def test_has_puppet(self):
        target, _, _ = await build_target()
        puppets = target.children_of_class(PicobootPuppet)
        assert len(puppets) == 1
        assert puppets[0].ram.address == 0x20000000
        # Top 4 KiB reserved for bootrom workspace.
        assert puppets[0].ram.size == 0x42000 - 0x1000

    @pytest.mark.asyncio
    async def test_target_name_includes_serial(self):
        # Picoboot under a parent named "rp2040-bootsel-SERIAL" gets
        # the serial reflected in the target name.
        from acrobe.node import Node
        transport = MockPicobootTransport()
        picoboot = Picoboot(transport)
        parent = Node("rp2040-bootsel-cafe1234")
        parent.child_add(picoboot)
        from acrobe.target.arm.rp2040 import rp2040_picoboot_probe
        target = await rp2040_picoboot_probe(picoboot)
        assert target.name == "rp2040-cafe1234"


class TestProgrammingRoute:
    @pytest.mark.asyncio
    async def test_program_erases_then_writes(self):
        target, _, transport = await build_target()
        from acrobe.target import Loadable
        from acrobe.memory_map import MemoryMap
        loadable = target.children_of_class(Loadable)[0]

        m = MemoryMap()
        m.append(XIP_BASE, b"\x55" * 256)
        await loadable.write(m, do_erase=True)

        # Exclusive access taken, then released.
        assert transport.exclusive_log == [1, 0]
        # Erase covered the one 4K sector that contains the written
        # range, and a single 256-byte page write landed at the start.
        assert transport.erase_log == [(XIP_BASE, 4096)]
        # First write at the data start.
        assert (XIP_BASE, b"\x55" * 256) in transport.write_log

    @pytest.mark.asyncio
    async def test_program_with_start_reboots(self):
        target, _, transport = await build_target()
        from acrobe.target import Loadable
        from acrobe.memory_map import MemoryMap
        loadable = target.children_of_class(Loadable)[0]

        m = MemoryMap()
        m.append(XIP_BASE, b"\xaa" * 256)
        await loadable.write(m, do_start=True)

        # Reboot was invoked with pc=0 (bootrom interprets as
        # "boot from flash").
        assert len(transport.reboots) == 1
        pc, sp, delay = transport.reboots[0]
        assert pc == 0 and sp == 0

    @pytest.mark.asyncio
    async def test_read_passes_through(self):
        target, _, transport = await build_target()
        from acrobe.target import Loadable
        loadable = target.children_of_class(Loadable)[0]
        transport.flash[0:8] = b"\xde\xad\xbe\xef\xca\xfe\xba\xbe"
        m = await loadable.read(begin=XIP_BASE, end=XIP_BASE + 8)
        # MemoryMap iterates as (addr, bytes).
        chunks = list(m)
        assert chunks == [(XIP_BASE, b"\xde\xad\xbe\xef\xca\xfe\xba\xbe")]
