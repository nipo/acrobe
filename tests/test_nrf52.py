"""Tests for the nRF52 family target."""

import asyncio
import pytest

from acrobe.component.arm.coresight.model import ComponentIds
from acrobe.component.arm.coresight.scs import Scs
from acrobe.component.arm.dp import DpAccessFailure
from acrobe.component.arm.mem_ap import MemAp
from acrobe.component.nordic.ctrl_ap import CtrlAp
from acrobe.db import NoMatch
from acrobe.node import Node
from acrobe.target import Loadable, Target
from acrobe.target.arm.cortex_m import CortexMDebuggable
from acrobe.target.debuggable import Debuggable
from acrobe.allocator import Allocator
from acrobe.target.arm.nrf52 import (
    FICR_CODEPAGESIZE, FICR_CODESIZE, FICR_DEVICEADDR0, FICR_DEVICEADDR1,
    FICR_INFO_PART, FICR_INFO_RAM, NRF52_PARTS, NRF_STUBS,
    NVMC_CONFIG, NVMC_CONFIG_EEN, NVMC_CONFIG_REN, NVMC_CONFIG_WEN,
    NVMC_ERASEPAGE, NVMC_ERASEUICR, NVMC_READY, NVMC_READY_BIT,
    UICR_BASE,
    Nrf52Loadable, Nrf52Target, NvmcFlash, UicrFlash, nrf52_probe,
)


class MockAp(MemAp):
    """Mem-AP mock. Bypasses MemAp's heavy init (CSW / TAR / batcher)
    by going straight to Node.__init__; the discovery probe checks
    `isinstance(x, MemAp)`, so being a proper subclass matters.

    Backs flash storage at 0x00000000 and tracks every
    read32/write32/mem_read/mem_write for assertions. NVMC.READY
    always reads as ready (bit 0 set) so polling returns
    immediately — keeps tests fast."""

    def __init__(self, name="ap", *, flash_size=0x100000, page_size=0x1000,
                 part=0x52840, ram_kb=256):
        Node.__init__(self, name)
        self.flash = bytearray(b"\xff" * flash_size)
        self.flash_size = flash_size
        self.page_size = page_size
        self.part = part
        self.ram_kb = ram_kb
        self.config = NVMC_CONFIG_REN
        self.config_history: list[int] = []
        self.erased_pages: list[int] = []
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int]] = []
        self.mem_writes: list[tuple[int, bytes]] = []
        self.ficr_part_fails = False

    def __future(self, value):
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        fut.set_result(value)
        return fut

    def __future_exc(self, exc):
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        fut.set_exception(exc)
        return fut

    def read32(self, addr):
        self.reads.append((addr, 0))
        if addr == FICR_INFO_PART:
            if self.ficr_part_fails:
                return self.__future_exc(DpAccessFailure("part fail"))
            return self.__future(self.part)
        if addr == FICR_CODEPAGESIZE:
            return self.__future(self.page_size)
        if addr == FICR_CODESIZE:
            return self.__future(self.flash_size // self.page_size)
        if addr == FICR_INFO_RAM:
            return self.__future(self.ram_kb)
        if addr == FICR_DEVICEADDR0 or addr == FICR_DEVICEADDR1:
            return self.__future(0)
        if addr == NVMC_READY:
            return self.__future(NVMC_READY_BIT)
        if 0 <= addr < self.flash_size:
            value = int.from_bytes(
                bytes(self.flash[addr:addr + 4]), "little")
            return self.__future(value)
        return self.__future(0)

    def write32(self, addr, data):
        self.writes.append((addr, data))
        if addr == NVMC_CONFIG:
            self.config = data
            self.config_history.append(data)
        elif addr == NVMC_ERASEPAGE:
            self.erased_pages.append(data)
            page_off = data
            self.flash[page_off:page_off + self.page_size] = (
                b"\xff" * self.page_size)
        return self.__future(None)

    def mem_read(self, addr, size):
        if 0 <= addr < self.flash_size:
            return self.__future(bytes(self.flash[addr:addr + size]))
        return self.__future(b"\x00" * size)

    def mem_write(self, addr, data):
        self.mem_writes.append((addr, bytes(data)))
        if 0 <= addr < self.flash_size:
            self.flash[addr:addr + len(data)] = data
        return self.__future(None)


# Recording mock for the puppet substrate. Treats `NRF_STUBS` blobs
# by identity — `flash_erase` zeros pages of the MockAp's flash,
# `flash_write` copies bytes out of the puppet's RAM buffer into
# flash. Tests assert against `stub_calls`, `erased_pages`, and the
# resulting `flash` content.
class MockPuppet:
    RAM_BASE = 0x20000000
    RAM_SIZE = 0x10000

    def __init__(self, mock_ap):
        self.ap = mock_ap
        self.ram = bytearray(self.RAM_SIZE)
        self.allocator = Allocator(self.RAM_BASE, self.RAM_SIZE)
        self.stub_calls: list[tuple[str, tuple]] = []

    def allocate(self, size, align=1):
        return MockZone(self, self.allocator.allocate(size, align))

    def unallocate(self, zone):
        self.allocator.free(zone.range)

    def stub(self, code, *, name="stub"):
        return MockPuppetStub(self, code, name)


class MockZone:
    def __init__(self, puppet, range_):
        self.puppet = puppet
        self.range = range_

    @property
    def address(self):
        return self.range.address

    @property
    def size(self):
        return self.range.size

    async def write(self, data, offset=0):
        ram_off = self.address + offset - self.puppet.RAM_BASE
        self.puppet.ram[ram_off:ram_off + len(data)] = data


class MockPuppetStub:
    def __init__(self, puppet, code, name):
        self.puppet = puppet
        self.code = code
        self.name = name

    async def call(self, *args, timeout=None):
        self.puppet.stub_calls.append((self.name, args))
        if self.code is NRF_STUBS["flash_erase"]:
            addr, size, page_size = args
            for p in range(addr, addr + size, page_size):
                self.puppet.ap.erased_pages.append(p)
                self.puppet.ap.flash[p:p + page_size] = (
                    b"\xff" * page_size)
        elif self.code is NRF_STUBS["flash_write"]:
            dst, src_addr, length = args
            src_off = src_addr - self.puppet.RAM_BASE
            data = bytes(self.puppet.ram[src_off:src_off + length])
            self.puppet.ap.flash[dst:dst + length] = data
            self.puppet.ap.mem_writes.append((dst, data))
        else:
            raise AssertionError(f"unknown stub {self.name!r}")

    def cleanup(self):
        pass


# -- NvmcFlash -------------------------------------------------------

class TestNvmcFlash:
    def make(self, *, flash_size=0x100000, page_size=0x1000):
        ap = MockAp(flash_size=flash_size, page_size=page_size)
        puppet = MockPuppet(ap)
        flash = NvmcFlash("code", 0, ap.flash_size, ap, puppet,
                          page_size=page_size)
        return ap, puppet, flash

    @pytest.mark.asyncio
    async def test_read_passes_through(self):
        ap, _, f = self.make()
        ap.flash[0:4] = b"\xde\xad\xbe\xef"
        assert await f.read(0, 4) == b"\xde\xad\xbe\xef"

    @pytest.mark.asyncio
    async def test_erase_one_page_calls_stub(self):
        ap, puppet, f = self.make()
        ap.flash[0x100:0x104] = b"\x11\x22\x33\x44"
        await f.erase(0, 0x1000)
        # One stub call: flash_erase(addr=0, size=0x1000, page=0x1000).
        assert puppet.stub_calls == [("nvmc_erase", (0, 0x1000, 0x1000))]
        # Mock-side effect: that page is now blank.
        assert ap.flash[0:4] == b"\xff\xff\xff\xff"
        assert ap.erased_pages == [0]

    @pytest.mark.asyncio
    async def test_erase_multiple_pages_one_stub_call(self):
        ap, puppet, f = self.make()
        await f.erase(0x1000, 0x3000)
        # Whole multi-page range collapses to a single stub call —
        # the stub loops on-target, no per-page host round-trip.
        assert puppet.stub_calls == [
            ("nvmc_erase", (0x1000, 0x3000, 0x1000)),
        ]
        assert ap.erased_pages == [0x1000, 0x2000, 0x3000]

    @pytest.mark.asyncio
    async def test_erase_full_size_marks_blank(self):
        _, _, f = self.make(flash_size=0x2000)
        assert not f.is_blank
        await f.erase(0, 0x2000)
        assert f.is_blank

    @pytest.mark.asyncio
    async def test_erase_unaligned_rejected(self):
        _, _, f = self.make()
        with pytest.raises(ValueError):
            await f.erase(0x100, 0x1000)
        with pytest.raises(ValueError):
            await f.erase(0, 0x500)

    @pytest.mark.asyncio
    async def test_write_calls_stub_with_ram_buffer(self):
        ap, puppet, f = self.make()
        await f.write(0x100, b"\xaa\xbb\xcc\xdd")
        # One stub call: flash_write(dst, src_buf, len). src_buf is a
        # RAM address inside the puppet's allocated zone.
        assert len(puppet.stub_calls) == 1
        name, (dst, src, length) = puppet.stub_calls[0]
        assert name == "nvmc_write"
        assert dst == 0x100
        assert length == 4
        assert MockPuppet.RAM_BASE <= src < MockPuppet.RAM_BASE + MockPuppet.RAM_SIZE
        # Mock-side effect: flash now holds the bytes the stub read
        # out of the puppet's RAM buffer.
        assert bytes(ap.flash[0x100:0x104]) == b"\xaa\xbb\xcc\xdd"
        assert ap.mem_writes == [(0x100, b"\xaa\xbb\xcc\xdd")]

    @pytest.mark.asyncio
    async def test_write_unaligned_rejected(self):
        _, _, f = self.make()
        with pytest.raises(ValueError):
            await f.write(0x100, b"\xaa\xbb\xcc")
        with pytest.raises(ValueError):
            await f.write(0x101, b"\xaa\xbb\xcc\xdd")

    @pytest.mark.asyncio
    async def test_write_frees_buffer_on_exception(self):
        ap, puppet, f = self.make()

        async def boom(*args, **kwargs):
            raise RuntimeError("stub failure")

        # Patch the install path so the stub call raises.
        puppet.stub = lambda code, *, name="stub": type(
            "BoomStub", (), {"call": boom, "cleanup": lambda self: None})()
        with pytest.raises(RuntimeError):
            await f.write(0x100, b"\xaa\xbb\xcc\xdd")
        # Buffer was released back to the allocator (next alloc fits).
        z = puppet.allocate(0x1000, align=4)
        assert z.size == 0x1000


# -- Probe / Target build --------------------------------------------

class FakeDp(Node):
    """Minimal DP stub for the discovery probe."""

    def __init__(self, name="dp"):
        super().__init__(name)


class FakeCtrlAp(CtrlAp):
    """CtrlAp subclass that bypasses the heavy Ap init (which wants
    a real DP for AP register I/O), and replaces the CTRL-AP register
    surface with bookkeeping the tests can assert against."""

    def __init__(self, name="ctrl-ap1"):
        Node.__init__(self, name)
        self.locked = False
        self.erased = False
        self.reset_history = []

    async def is_protected(self):
        return self.locked

    async def erase_all(self, *, timeout=10.0):
        self.erased = True
        self.locked = False

    async def release_reset(self):
        self.reset_history.append("release_reset")


def _make_rom_table_with_scs(ap):
    """Build a ROM-Table-shaped subtree containing an SCS under `ap`."""
    from acrobe.component.arm.coresight.rom_table import RomTable
    rt = RomTable(ap, 0xE00FF000, ComponentIds.empty())
    ap._child_attach(rt)
    scs = Scs(ap, 0xE000E000, ComponentIds.empty())
    rt._child_attach(scs)
    return rt


class TestNrf52Probe:
    @pytest.mark.asyncio
    async def test_known_part_spawns_target(self):
        dp = FakeDp()
        ap = MockAp(part=0x52840, flash_size=0x100000, page_size=0x1000)
        dp._child_attach(ap)
        _make_rom_table_with_scs(ap)
        target = await nrf52_probe(dp)
        assert isinstance(target, Nrf52Target)
        # Suffixed with the FICR BLE address (zeros under MockAp).
        assert target.name.startswith("nRF52840-")
        debuggables = target.children_of_class(CortexMDebuggable)
        assert len(debuggables) == 1
        loadables = target.children_of_class(Loadable)
        assert len(loadables) == 1
        flashes = loadables[0].children_of_class(NvmcFlash)
        # One main code flash + one UICR.
        assert len(flashes) == 2
        names = {f.name for f in flashes}
        assert names == {"code", "uicr"}
        code = next(f for f in flashes if f.name == "code")
        assert code.size == 0x100000
        assert code.write_page_size == 0x1000

    @pytest.mark.asyncio
    async def test_memory_map_covers_ram_ficr_peripherals_ppb(self):
        """GDB needs every range we want the user to inspect declared
        in qXfer:memory-map, otherwise its memory-map clamping rejects
        out-of-region reads before sending an `m` packet."""
        from acrobe.target.region import Ram

        dp = FakeDp()
        ap = MockAp(part=0x52840)
        dp._child_attach(ap)
        _make_rom_table_with_scs(ap)
        target = await nrf52_probe(dp)
        debug = target.children_of_class(CortexMDebuggable)[0]
        ranges = {r.name: (r.address, r.size)
                  for r in debug.memory_map if isinstance(r, Ram)}
        assert ranges["ppb"]  == (0xE0000000, 0x100000)  # from CortexM default
        assert ranges["sram"] == (0x20000000, 256 * 1024)
        assert ranges["ficr"] == (0x10000000, 0x1000)
        assert ranges["apb"]  == (0x40000000, 0x80000)
        assert ranges["ahb"]  == (0x50000000, 0x80000)

    @pytest.mark.asyncio
    async def test_unknown_part_declines(self):
        dp = FakeDp()
        ap = MockAp(part=0xDEADBEEF)
        dp._child_attach(ap)
        _make_rom_table_with_scs(ap)
        with pytest.raises(NoMatch):
            await nrf52_probe(dp)

    @pytest.mark.asyncio
    async def test_ficr_failure_declines(self):
        dp = FakeDp()
        ap = MockAp()
        ap.ficr_part_fails = True
        dp._child_attach(ap)
        _make_rom_table_with_scs(ap)
        with pytest.raises(NoMatch):
            await nrf52_probe(dp)

    @pytest.mark.asyncio
    async def test_no_memap_declines(self):
        dp = FakeDp()
        with pytest.raises(NoMatch):
            await nrf52_probe(dp)

    @pytest.mark.asyncio
    async def test_no_scs_under_memap_declines(self):
        dp = FakeDp()
        ap = MockAp()
        dp._child_attach(ap)
        # No ROM table; no SCS.
        with pytest.raises(NoMatch):
            await nrf52_probe(dp)


class TestUicrFlash:
    @pytest.mark.asyncio
    async def test_erase_writes_eraseuicr(self):
        ap = MockAp()
        puppet = MockPuppet(ap)
        f = UicrFlash("uicr", UICR_BASE, 0x1000, ap, puppet, page_size=0x1000)
        await f.erase(0, 0x1000)
        # ERASEUICR was written with 1 (MMIO one-shot, no stub).
        assert (NVMC_ERASEUICR, 1) in ap.writes
        assert puppet.stub_calls == []
        # Region marked blank.
        assert f.is_blank

    @pytest.mark.asyncio
    async def test_partial_erase_rejected(self):
        ap = MockAp()
        puppet = MockPuppet(ap)
        f = UicrFlash("uicr", UICR_BASE, 0x1000, ap, puppet, page_size=0x1000)
        with pytest.raises(ValueError):
            await f.erase(0, 0x800)


class FakeCore:
    def __init__(self):
        self.history = []

    async def halt(self):
        self.history.append("halt")

    async def reset(self, *, stop=True):
        self.history.append(f"reset(stop={stop})")


class FakeDebuggable:
    """Stub Debuggable that the Nrf52Loadable's __core() helper
    walks to. Has the same shape as the real one."""

    def __init__(self, core):
        self.core = core
        self.cores = [core]

    def children_of_class(self, klass):
        return []


class _FakeTarget(Node):
    """Minimal Node parent that exposes a Debuggable child for the
    Loadable's __core() helper. Standalone — does not run through
    discovery."""

    def __init__(self, name, core):
        super().__init__(name)
        from acrobe.target.debuggable import Debuggable
        self.debuggable = Debuggable("debug")
        self._child_attach(self.debuggable)
        from acrobe.target.debuggable import Core
        # Hot-patch the Debuggable to expose .cores attribute.
        # Real Debuggable .cores walks children; we attach `core`
        # as a real Node child of the Debuggable.
        self.debuggable._child_attach(core)


class HaltableCore(Node):
    """Minimal Core-shaped Node we can attach under a Debuggable."""

    from acrobe.target.debuggable import Core as _Core

    def __init__(self):
        Node.__init__(self, "core")
        self.history = []

    async def halt(self):
        self.history.append("halt")

    async def reset(self, *, stop=True):
        self.history.append(f"reset(stop={stop})")


class TestNrf52LoadableHalt:
    """Pre-program halts the CPU; post-program optionally resets."""

    def make(self, *, ctrl_ap=None):
        from acrobe.target.debuggable import Core, Debuggable

        # Build a stand-alone tree: Target → Debuggable → Core.
        # We pass the real Debuggable/Core classes plus a "core" that
        # records halt/reset calls.
        class RecordingCore(Core):
            def __init__(self):
                Core.__init__(self, "core")
                self.history = []

            async def halt(self):
                self.history.append("halt")

            async def reset(self, *, stop=True):
                self.history.append(f"reset(stop={stop})")

        core = RecordingCore()
        debug = Debuggable("debug")
        debug.child_add(core)

        target = Target("nRF52840")
        target.child_add(debug)

        loadable = Nrf52Loadable("main", ctrl_ap=ctrl_ap)
        target.child_add(loadable)
        return loadable, core

    @pytest.mark.asyncio
    async def test_pre_program_halts_cpu(self):
        loadable, core = self.make()
        await loadable.pre_program(do_erase=False, assume_clean=False)
        assert core.history == ["halt"]

    @pytest.mark.asyncio
    async def test_post_program_reset_on_do_start(self):
        loadable, core = self.make()
        await loadable.post_program(success=True, do_start=True)
        assert core.history == ["reset(stop=False)"]

    @pytest.mark.asyncio
    async def test_post_program_no_reset_on_failure(self):
        loadable, core = self.make()
        await loadable.post_program(success=False, do_start=True)
        assert core.history == []

    @pytest.mark.asyncio
    async def test_no_debuggable_pre_program_is_noop(self):
        target = Target("bare")
        loadable = Nrf52Loadable("main")
        target.child_add(loadable)
        # No Debuggable sibling — pre_program must not crash.
        await loadable.pre_program(do_erase=False, assume_clean=False)


class TestNrf52LoadableEraseAll:
    @pytest.mark.asyncio
    async def test_uses_ctrl_ap_when_available(self):
        from acrobe.target.debuggable import Core, Debuggable

        class RecordingCore(Core):
            def __init__(self):
                Core.__init__(self, "core")
                self.history = []

            async def halt(self):
                self.history.append("halt")

            async def reset(self, *, stop=True):
                self.history.append(f"reset(stop={stop})")

        ctrl_ap = FakeCtrlAp()
        loadable = Nrf52Loadable("main", ctrl_ap=ctrl_ap)
        target = Target("nRF52840")
        target.child_add(loadable)
        debug = Debuggable("debug")
        core = RecordingCore()
        debug.child_add(core)
        target.child_add(debug)

        ap = MockAp()
        puppet = MockPuppet(ap)
        loadable.child_add(
            NvmcFlash("code", 0, 0x1000, ap, puppet, page_size=0x1000))

        await loadable.erase_all()

        # CTRL-AP did the heavy lifting; per-page NVMC erase did NOT
        # run (no ERASEPAGE writes).
        assert ctrl_ap.erased
        assert ap.erased_pages == []
        # Reset was cycled.
        assert "release_reset" in ctrl_ap.reset_history
        assert "reset(stop=True)" in core.history
        # All flashes marked blank.
        for f in loadable.children_of_class(NvmcFlash):
            assert f.is_blank

    @pytest.mark.asyncio
    async def test_falls_back_to_nvmc_when_no_ctrl_ap(self):
        ap = MockAp()
        puppet = MockPuppet(ap)
        loadable = Nrf52Loadable("main", ctrl_ap=None)
        target = Target("nRF52840")
        target.child_add(loadable)
        loadable.child_add(
            NvmcFlash("code", 0, 0x2000, ap, puppet, page_size=0x1000))
        await loadable.erase_all()
        # Two pages erased through the on-target stub.
        assert ap.erased_pages == [0, 0x1000]
        assert puppet.stub_calls == [("nvmc_erase", (0, 0x2000, 0x1000))]


class TestApprotect:
    """Probe path when CTRL-AP reports APPROTECT engaged."""

    @pytest.mark.asyncio
    async def test_locked_chip_spawns_partial_target(self):
        dp = FakeDp()
        ctrl_ap = FakeCtrlAp()
        ctrl_ap.locked = True
        dp._child_attach(ctrl_ap)
        # No Mem-AP attached at all — the locked path mustn't touch it.
        target = await nrf52_probe(dp)
        assert isinstance(target, Nrf52Target)
        assert "locked" in target.name.lower()
        # Locked target: no Debuggable, one Loadable with no regions.
        assert target.children_of_class(Debuggable) == []
        loadables = target.children_of_class(Loadable)
        assert len(loadables) == 1
        assert loadables[0].locked is True
        assert loadables[0].children_of_class(NvmcFlash) == []

    @pytest.mark.asyncio
    async def test_locked_write_without_erase_raises(self):
        ctrl_ap = FakeCtrlAp()
        ctrl_ap.locked = True
        loadable = Nrf52Loadable("main", ctrl_ap=ctrl_ap, locked=True)
        target = Target("nRF52 (locked)")
        target.child_add(loadable)
        from acrobe.memory_map import MemoryMap
        m = MemoryMap()
        m.append(0, b"\x00" * 16)
        with pytest.raises(RuntimeError, match="APPROTECT"):
            await loadable.write(m, do_erase=False)
        assert not ctrl_ap.erased

    @pytest.mark.asyncio
    async def test_locked_write_with_erase_runs_erase_all_and_stops(self):
        ctrl_ap = FakeCtrlAp()
        ctrl_ap.locked = True
        loadable = Nrf52Loadable("main", ctrl_ap=ctrl_ap, locked=True)
        target = Target("nRF52 (locked)")
        target.child_add(loadable)
        from acrobe.memory_map import MemoryMap
        m = MemoryMap()
        m.append(0, b"\x00" * 16)
        await loadable.write(m, do_erase=True)
        # erase_all via CTRL-AP did run; no regions to program.
        assert ctrl_ap.erased

    @pytest.mark.asyncio
    async def test_unlocked_takes_normal_path(self):
        """CTRL-AP present but APPROTECT clear → normal Target build."""
        dp = FakeDp()
        ctrl_ap = FakeCtrlAp()
        ctrl_ap.locked = False
        dp._child_attach(ctrl_ap)
        ap = MockAp(part=0x52840, flash_size=0x10000, page_size=0x1000)
        dp._child_attach(ap)
        _make_rom_table_with_scs(ap)
        target = await nrf52_probe(dp)
        # Normal target — has Debuggable + Flash regions.
        assert target.children_of_class(Debuggable) != []
        loadables = target.children_of_class(Loadable)
        assert loadables[0].locked is False
        assert loadables[0].ctrl_ap is ctrl_ap

    @pytest.mark.asyncio
    async def test_is_protected_failure_falls_through(self):
        """If reading APPROTECTSTATUS itself raises, treat as
        unlocked and proceed normally — same as before this code
        path existed."""
        dp = FakeDp()
        ctrl_ap = FakeCtrlAp()

        async def boom():
            raise DpAccessFailure("transient")
        ctrl_ap.is_protected = boom

        dp._child_attach(ctrl_ap)
        ap = MockAp(part=0x52840)
        dp._child_attach(ap)
        _make_rom_table_with_scs(ap)
        target = await nrf52_probe(dp)
        assert target.children_of_class(Debuggable) != []


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_loadable_write_through_to_flash(self):
        """Loadable.write → plan_update → NvmcFlash.erase + .write.

        Builds the target by hand around a MockPuppet so the test
        exercises the real Loadable orchestration without booting
        the SCS / reg-write machinery the live puppet needs.
        """
        from acrobe.memory_map import MemoryMap

        ap = MockAp(part=0x52840, flash_size=0x10000, page_size=0x1000)
        puppet = MockPuppet(ap)
        loadable = Nrf52Loadable("main", ctrl_ap=None)
        target = Target("nRF52840")
        target.child_add(loadable)
        loadable.child_add(
            NvmcFlash("code", 0, 0x10000, ap, puppet, page_size=0x1000))

        m = MemoryMap()
        m.append(0x1000, b"\xab" * 16)
        await loadable.write(m, do_erase=False)

        # Page at 0x1000 was erased then written (two stub calls).
        assert puppet.stub_calls == [
            ("nvmc_erase", (0x1000, 0x1000, 0x1000)),
            ("nvmc_write", puppet.stub_calls[1][1]),  # write args asserted below
        ]
        _, (dst, _src, length) = puppet.stub_calls[1]
        assert dst == 0x1000
        assert length == 0x1000
        # First 16 bytes of the page reflect the input; tail filled
        # with 0xff by Flash.plan_update's paging.
        assert bytes(ap.flash[0x1000:0x1010]) == b"\xab" * 16
        assert bytes(ap.flash[0x1010:0x1020]) == b"\xff" * 16
