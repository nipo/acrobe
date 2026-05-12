"""Tests for the nRF52 family target."""

import asyncio
import pytest

from acrobe.component.arm.coresight.model import ComponentIds
from acrobe.component.arm.coresight.scs import Scs
from acrobe.component.arm.dp import DpAccessFailure
from acrobe.component.arm.mem_ap import MemAp
from acrobe.db import NoMatch
from acrobe.node import Node
from acrobe.target import Loadable, Target
from acrobe.target.arm.cortex_m import CortexMDebuggable
from acrobe.target.arm.nrf52 import (
    FICR_CODEPAGESIZE, FICR_CODESIZE, FICR_INFO_PART,
    NRF52_PARTS, NVMC_CONFIG, NVMC_CONFIG_EEN,
    NVMC_CONFIG_REN, NVMC_CONFIG_WEN, NVMC_ERASEPAGE,
    NVMC_ERASEUICR, NVMC_READY, NVMC_READY_BIT, UICR_BASE,
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
                 part=0x52840):
        Node.__init__(self, name)
        self.flash = bytearray(b"\xff" * flash_size)
        self.flash_size = flash_size
        self.page_size = page_size
        self.part = part
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
            if self.config != NVMC_CONFIG_EEN:
                raise RuntimeError(
                    "ERASEPAGE write while NVMC not in EEN mode")
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
        if self.config != NVMC_CONFIG_WEN:
            raise RuntimeError(
                "mem_write to flash while NVMC not in WEN mode")
        if 0 <= addr < self.flash_size:
            self.flash[addr:addr + len(data)] = data
        return self.__future(None)


# -- NvmcFlash -------------------------------------------------------

class TestNvmcFlash:
    @pytest.mark.asyncio
    async def test_read_passes_through(self):
        ap = MockAp()
        ap.flash[0:4] = b"\xde\xad\xbe\xef"
        f = NvmcFlash("code", 0, ap.flash_size, ap, page_size=0x1000)
        assert await f.read(0, 4) == b"\xde\xad\xbe\xef"

    @pytest.mark.asyncio
    async def test_erase_one_page_sequence(self):
        ap = MockAp()
        f = NvmcFlash("code", 0, ap.flash_size, ap, page_size=0x1000)
        ap.flash[0x100:0x104] = b"\x11\x22\x33\x44"
        await f.erase(0, 0x1000)
        # Config went EEN → REN.
        assert ap.config_history[0] == NVMC_CONFIG_EEN
        assert ap.config_history[-1] == NVMC_CONFIG_REN
        # ERASEPAGE was written for page 0.
        assert ap.erased_pages == [0]
        # Flash is erased.
        assert ap.flash[0:4] == b"\xff\xff\xff\xff"

    @pytest.mark.asyncio
    async def test_erase_multiple_pages(self):
        ap = MockAp()
        f = NvmcFlash("code", 0, ap.flash_size, ap, page_size=0x1000)
        await f.erase(0x1000, 0x3000)
        assert ap.erased_pages == [0x1000, 0x2000, 0x3000]

    @pytest.mark.asyncio
    async def test_erase_full_size_marks_blank(self):
        ap = MockAp(flash_size=0x2000, page_size=0x1000)
        f = NvmcFlash("code", 0, 0x2000, ap, page_size=0x1000)
        assert not f.is_blank
        await f.erase(0, 0x2000)
        assert f.is_blank

    @pytest.mark.asyncio
    async def test_erase_unaligned_rejected(self):
        ap = MockAp()
        f = NvmcFlash("code", 0, ap.flash_size, ap, page_size=0x1000)
        with pytest.raises(ValueError):
            await f.erase(0x100, 0x1000)
        with pytest.raises(ValueError):
            await f.erase(0, 0x500)

    @pytest.mark.asyncio
    async def test_write_sequence(self):
        ap = MockAp()
        f = NvmcFlash("code", 0, ap.flash_size, ap, page_size=0x1000)
        await f.write(0x100, b"\xaa\xbb\xcc\xdd")
        # Config flipped WEN → REN.
        assert NVMC_CONFIG_WEN in ap.config_history
        assert ap.config_history[-1] == NVMC_CONFIG_REN
        # mem_write was issued at flash offset 0x100.
        assert ap.mem_writes == [(0x100, b"\xaa\xbb\xcc\xdd")]
        # Flash storage reflects the write.
        assert ap.flash[0x100:0x104] == b"\xaa\xbb\xcc\xdd"

    @pytest.mark.asyncio
    async def test_write_unaligned_rejected(self):
        ap = MockAp()
        f = NvmcFlash("code", 0, ap.flash_size, ap, page_size=0x1000)
        with pytest.raises(ValueError):
            await f.write(0x100, b"\xaa\xbb\xcc")
        with pytest.raises(ValueError):
            await f.write(0x101, b"\xaa\xbb\xcc\xdd")

    @pytest.mark.asyncio
    async def test_write_restores_config_on_exception(self):
        ap = MockAp()

        original_mem_write = ap.mem_write

        def failing_mem_write(addr, data):
            ap.mem_writes.append((addr, bytes(data)))
            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            fut.set_exception(RuntimeError("bus error"))
            return fut

        ap.mem_write = failing_mem_write
        f = NvmcFlash("code", 0, ap.flash_size, ap, page_size=0x1000)
        with pytest.raises(RuntimeError):
            await f.write(0x100, b"\xaa\xbb\xcc\xdd")
        # CONFIG was restored to REN despite the failure.
        assert ap.config_history[-1] == NVMC_CONFIG_REN


# -- Probe / Target build --------------------------------------------

class FakeDp(Node):
    """Minimal DP stub for the discovery probe."""

    def __init__(self, name="dp"):
        super().__init__(name)


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
        assert target.name == "nRF52840"
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
        f = UicrFlash("uicr", UICR_BASE, 0x1000, ap, page_size=0x1000)
        await f.erase(0, 0x1000)
        # ERASEUICR was written with 1.
        assert (NVMC_ERASEUICR, 1) in ap.writes
        # Region marked blank.
        assert f.is_blank

    @pytest.mark.asyncio
    async def test_partial_erase_rejected(self):
        ap = MockAp()
        f = UicrFlash("uicr", UICR_BASE, 0x1000, ap, page_size=0x1000)
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


class FakeCtrlAp:
    """Stub CtrlAp for Nrf52Loadable.erase_all wiring tests."""

    def __init__(self):
        self.erased = False
        self.reset_history = []

    async def erase_all(self, *, timeout=10.0):
        self.erased = True

    async def release_reset(self):
        self.reset_history.append("release_reset")


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
        loadable.child_add(
            NvmcFlash("code", 0, 0x1000, ap, page_size=0x1000))

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
        loadable = Nrf52Loadable("main", ctrl_ap=None)
        target = Target("nRF52840")
        target.child_add(loadable)
        loadable.child_add(
            NvmcFlash("code", 0, 0x2000, ap, page_size=0x1000))
        await loadable.erase_all()
        # Two pages erased through NVMC.
        assert ap.erased_pages == [0, 0x1000]


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_loadable_write_through_to_flash(self):
        """Loadable.write → plan_update → NvmcFlash.erase + .write."""
        dp = FakeDp()
        ap = MockAp(part=0x52840, flash_size=0x10000, page_size=0x1000)
        dp._child_attach(ap)
        _make_rom_table_with_scs(ap)
        target = await nrf52_probe(dp)
        loadable = target.children_of_class(Loadable)[0]

        from acrobe.memory_map import MemoryMap
        m = MemoryMap()
        m.append(0x1000, b"\xab" * 16)
        await loadable.write(m, do_erase=False)

        # Page at 0x1000 was erased then written.
        assert 0x1000 in ap.erased_pages
        # The first 16 bytes of that page reflect the input.
        assert bytes(ap.flash[0x1000:0x1010]) == b"\xab" * 16
        # Rest of the page was filled with 0xff (paged() filler) but
        # since flash was just erased it's still 0xff anyway.
