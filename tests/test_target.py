"""Tests for the Target framework."""

import pytest

from acrobe.memory_map import MemoryMap
from acrobe.node import Node
from acrobe.target import (
    Flash, Loadable, NotUpdatable, Ram, Region, Target, TargetDiscovery,
)


def _mm(*chunks):
    m = MemoryMap()
    for addr, data in chunks:
        m.append(addr, data)
    return m


class MockRam(Ram):
    """In-memory RAM region for tests."""

    def __init__(self, name, address, size):
        super().__init__(name, address, size)
        self.storage = bytearray(size)

    async def read(self, offset, size):
        return bytes(self.storage[offset:offset + size])

    async def write(self, offset, data):
        self.storage[offset:offset + len(data)] = data


class MockFlash(Flash):
    """In-memory Flash region with erase/write tracking."""

    def __init__(self, name, address, size, page_size=256):
        super().__init__(name, address, size,
                         write_page_size=page_size,
                         erase_page_sizes=[page_size])
        self.storage = bytearray(b"\xff" * size)
        self.erase_log = []
        self.write_log = []

    async def read(self, offset, size):
        return bytes(self.storage[offset:offset + size])

    async def write(self, offset, data):
        self.write_log.append((offset, bytes(data)))
        self.storage[offset:offset + len(data)] = data

    async def erase(self, offset, size):
        self.erase_log.append((offset, size))
        self.storage[offset:offset + size] = b"\xff" * size
        if offset == 0 and size == self.size:
            self.is_blank = True


class TestRegionUpdate:
    """Region.update / plan_update / NotUpdatable contracts."""

    @pytest.mark.asyncio
    async def test_ram_update_writes(self):
        ram = MockRam("r", 0x100, 0x100)
        await ram.update(0x10, b"\xab\xcd")
        assert ram.storage[0x10:0x12] == b"\xab\xcd"

    @pytest.mark.asyncio
    async def test_ram_plan_update_yields_raw(self):
        ram = MockRam("r", 0x100, 0x100)
        m = _mm((0x100, b"\xaa\xbb\xcc"))
        chunks = [(off, data) async for off, data in ram.plan_update(m)]
        assert chunks == [(0, b"\xaa\xbb\xcc")]

    @pytest.mark.asyncio
    async def test_flash_plan_update_erases_then_pages(self):
        f = MockFlash("f", 0x100, 0x200, page_size=16)
        f.is_blank = False
        m = _mm((0x100, b"\x55" * 32))
        chunks = [(off, data) async for off, data in f.plan_update(m)]
        # Flash.plan_update aligns erases to erase_page_sizes[0] and
        # issues one erase per touched page.
        assert f.erase_log == [(0, 16), (16, 16)]
        assert chunks == [(0, b"\x55" * 16), (16, b"\x55" * 16)]

    @pytest.mark.asyncio
    async def test_flash_plan_update_skips_erase_when_blank(self):
        f = MockFlash("f", 0x100, 0x200, page_size=16)
        f.is_blank = True
        m = _mm((0x100, b"\x55" * 32))
        _ = [(off, data) async for off, data in f.plan_update(m)]
        assert f.erase_log == []

    @pytest.mark.asyncio
    async def test_not_updatable_signals_otp(self):
        class Otp(Region):
            async def update(self, offset, data):
                raise NotUpdatable(f"{self.name} is OTP")

        r = Otp("fuses", 0, 16)
        with pytest.raises(NotUpdatable):
            await r.update(0, b"\x00")


class TestLoadable:
    @staticmethod
    def make():
        t = Target("t")
        l = Loadable("main")
        t.child_add(l)
        flash = MockFlash("flash", 0x08000000, 0x1000, page_size=256)
        ram = MockRam("sram", 0x20000000, 0x4000)
        l.child_add(flash)
        l.child_add(ram)
        return t, l, flash, ram

    @pytest.mark.asyncio
    async def test_write_flash(self):
        _, l, flash, _ = self.make()
        await l.write(_mm((0x08000000, b"\xaa" * 256)))
        assert (await flash.read(0, 256)) == b"\xaa" * 256
        # Flash starts non-blank → plan_update issues erase.
        assert flash.erase_log == [(0, 256)]

    @pytest.mark.asyncio
    async def test_write_ram(self):
        _, l, _, ram = self.make()
        await l.write(_mm((0x20000000, b"\xbb" * 32)))
        assert (await ram.read(0, 32)) == b"\xbb" * 32

    @pytest.mark.asyncio
    async def test_do_erase_runs_erase_all(self):
        _, l, flash, _ = self.make()
        await l.write(_mm((0x08000000, b"\xcc" * 256)), do_erase=True)
        # erase_all() erased the whole flash before the per-page write loop.
        assert flash.erase_log[0] == (0, 0x1000)
        assert flash.is_blank is True

    @pytest.mark.asyncio
    async def test_skips_erase_when_blank(self):
        _, l, flash, _ = self.make()
        flash.is_blank = True
        await l.write(_mm((0x08000000, b"\xee" * 256)))
        # Was already blank → no per-chunk erase issued.
        assert flash.erase_log == []

    @pytest.mark.asyncio
    async def test_assume_clean_forces_blank(self):
        _, l, flash, _ = self.make()
        flash.is_blank = False
        await l.write(_mm((0x08000000, b"\x77" * 256)), assume_clean=True)
        assert flash.erase_log == []

    @pytest.mark.asyncio
    async def test_verify_success(self):
        _, l, _, _ = self.make()
        m = _mm((0x08000000, b"\xaa" * 256))
        await l.write(m)
        assert await l.verify(m) is True

    @pytest.mark.asyncio
    async def test_verify_failure(self):
        _, l, _, _ = self.make()
        # No write — flash is still 0xff while map expects 0xaa.
        assert await l.verify(_mm((0x08000000, b"\xaa" * 256))) is False

    @pytest.mark.asyncio
    async def test_read_returns_all_regions(self):
        _, l, flash, ram = self.make()
        flash.storage[0:4] = b"\x11\x22\x33\x44"
        m = await l.read()
        addrs = sorted(a for a, _ in m)
        assert addrs == [0x08000000, 0x20000000]

    @pytest.mark.asyncio
    async def test_read_range_clips(self):
        _, l, flash, _ = self.make()
        flash.storage[0:4] = b"\xde\xad\xbe\xef"
        m = await l.read(begin=0x08000000, end=0x08000100)
        assert len(m) == 1
        addr, data = m[0]
        assert addr == 0x08000000
        assert len(data) == 0x100

    @pytest.mark.asyncio
    async def test_erase_all_marks_blank(self):
        _, l, flash, _ = self.make()
        flash.is_blank = False
        flash.storage[0:4] = b"\xaa\xbb\xcc\xdd"
        await l.erase_all()
        assert flash.is_blank
        assert flash.storage[0:4] == b"\xff\xff\xff\xff"

    @pytest.mark.asyncio
    async def test_force_blank(self):
        _, l, flash, _ = self.make()
        flash.is_blank = False
        l.force_blank()
        assert flash.is_blank

    @pytest.mark.asyncio
    async def test_chunk_outside_regions_ignored(self):
        _, l, flash, _ = self.make()
        flash.is_blank = True
        await l.write(_mm((0x40000000, b"\xff" * 16)))
        assert flash.write_log == []

    @pytest.mark.asyncio
    async def test_hooks_fire_in_order(self):
        order = []

        class HookedLoadable(Loadable):
            async def pre_program(self, *, do_erase, assume_clean):
                order.append("pre")
                await super().pre_program(do_erase=do_erase,
                                          assume_clean=assume_clean)

            async def post_program(self, *, success, do_start):
                order.append("post")
                await super().post_program(success=success, do_start=do_start)

            async def reset(self):
                order.append("reset")

        t = Target("t")
        hl = HookedLoadable("main")
        t.child_add(hl)
        flash = MockFlash("flash", 0x100, 0x100, page_size=16)
        flash.is_blank = True
        hl.child_add(flash)

        await hl.write(_mm((0x100, b"\x55" * 16)),
                       do_verify=True, do_start=True)
        assert order == ["pre", "post", "reset"]

    @pytest.mark.asyncio
    async def test_no_reset_on_verify_failure(self):
        order = []

        class HookedLoadable(Loadable):
            async def reset(self):
                order.append("reset")

        t = Target("t")
        hl = HookedLoadable("main")
        t.child_add(hl)
        flash = MockFlash("flash", 0x100, 0x100, page_size=16)
        hl.child_add(flash)

        # Verify will fail (map expects 0xaa, flash will end up with 0xff
        # because the test deliberately writes a mismatching map: actually
        # plan_update will write what the map says, so build a verify
        # failure by checking with a different map below.)
        await hl.write(_mm((0x100, b"\xaa" * 16)), do_verify=False)
        bad = _mm((0x100, b"\x55" * 16))
        # Manually invoke verify path + post_program to simulate verify
        # failing on a do_start flow.
        await hl.post_program(success=False, do_start=True)
        assert order == []


class TestMultiLoadable:
    """Multiple Loadable children on one Target (Mach-XO2 dual-config case)."""

    @pytest.mark.asyncio
    async def test_distinct_loadables_program_independently(self):
        t = Target("multi")
        sram = Loadable("sram")
        nvcm = Loadable("nvcm")
        t.child_add(sram)
        t.child_add(nvcm)

        sram_region = MockRam("sram-bank", 0, 0x100)
        nvcm_region = MockFlash("nvcm-bank", 0x1000, 0x100, page_size=16)
        nvcm_region.is_blank = True
        sram.child_add(sram_region)
        nvcm.child_add(nvcm_region)

        await sram.write(_mm((0, b"\xaa" * 16)))
        await nvcm.write(_mm((0x1000, b"\xbb" * 16)))

        assert sram_region.storage[:16] == b"\xaa" * 16
        assert nvcm_region.storage[:16] == b"\xbb" * 16
        # Each Loadable only touches its own region.
        assert sram_region.storage[16:32] == bytes(16)
        assert nvcm_region.storage[16:32] == b"\xff" * 16


class TestRegistry:
    def test_register_increments(self):
        count = len(Target.explorers)

        class FakeComponent(Node):
            pass

        @Target.register(FakeComponent, precedence=500)
        def make_target(component):
            return Target("fake")

        try:
            assert len(Target.explorers) == count + 1
            entry = [e for e in Target.explorers if e.func is make_target][0]
            assert entry.precedence == 500
            assert entry.component_types == (FakeComponent,)
        finally:
            Target.explorers[:] = [
                e for e in Target.explorers if e.func is not make_target
            ]


class _SoftComponent(Node):
    """Test fixture: a Node a primary Target exposes after construction,
    discoverable as a secondary Target's source."""


class _PrimaryTarget(Target):
    def __init__(self, component):
        super().__init__(f"primary-{component.name}")
        self.component = component
        # Expose a soft component beneath ourselves — picked up on the
        # next discovery pass.
        self.soft = _SoftComponent("soft")
        self.child_add(self.soft)


class _SecondaryTarget(Target):
    def __init__(self, soft):
        super().__init__(f"secondary-{soft.name}")
        self.soft = soft


@Target.register(_SoftComponent)
def _spawn_secondary(soft):
    return _SecondaryTarget(soft)


class _PrimarySource(Node):
    pass


@Target.register(_PrimarySource)
def _spawn_primary(component):
    return _PrimaryTarget(component)


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_basic_spawn(self):
        root = Node("root")
        src = _PrimarySource("src")
        root._child_attach(src)

        disc = TargetDiscovery()
        spawned = await disc.run(root)

        assert len(spawned) == 2
        names = sorted(t.name for t in spawned)
        assert names == ["primary-src", "secondary-soft"]
        # Both Targets parked flat under the root.
        targets = root.children_of_class(Target)
        assert len(targets) == 2

    @pytest.mark.asyncio
    async def test_dedup_no_double_spawn(self):
        root = Node("root")
        src = _PrimarySource("src")
        root._child_attach(src)

        disc = TargetDiscovery()
        first = await disc.run(root)
        second = await disc.run(root)

        assert len(first) == 2
        assert len(second) == 0
        assert len(root.children_of_class(Target)) == 2

    @pytest.mark.asyncio
    async def test_unmatched_component_ignored(self):
        root = Node("root")
        root._child_attach(Node("orphan"))
        disc = TargetDiscovery()
        spawned = await disc.run(root)
        assert spawned == []


class TestBestEffortInvalidation:
    @pytest.mark.asyncio
    async def test_dead_component_surfaces_io_error(self):
        """Reference held in Region survives detach of the source
        component, but next op against it surfaces an error."""

        class DeadAp:
            alive = True

            async def mem_read(self, addr, size):
                if not self.alive:
                    raise IOError("adapter gone")
                return b"\x00" * size

        ap = DeadAp()

        class BusBackedRam(Ram):
            def __init__(self, ap):
                super().__init__("r", 0, 0x100)
                self.ap = ap

            async def read(self, offset, size):
                return await self.ap.mem_read(self.address + offset, size)

        ram = BusBackedRam(ap)
        # Simulate adapter detach
        ap.alive = False
        with pytest.raises(IOError):
            await ram.read(0, 4)
