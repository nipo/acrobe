"""Linux /dev/mem windows (`acrobe.adapter.linux.devmem`).

A window is an mmap of whatever file it is given, so a page-sized
regular file with a known pattern stands in for ``/dev/mem`` and
exercises every access path without root.
"""

import asyncio
import mmap
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux /dev/mem semantics")

from acrobe.adapter.linux.devmem import (  # noqa: E402
    DevMemBroker, DevMemEnumerator, DevMemWindow, _parse_window,
)
from acrobe.adapter.model import HwRoot  # noqa: E402
from acrobe.db import NoMatch  # noqa: E402
from acrobe.lifecycle import lifecycle  # noqa: E402
from acrobe.node import Node  # noqa: E402
from acrobe.protocol import memory  # noqa: E402


GRAN = mmap.ALLOCATIONGRANULARITY
PAGES = 3
PATTERN = bytes((i * 7 + 3) & 0xff for i in range(GRAN * PAGES))


@pytest.fixture
def backing(tmp_path):
    """A whole-page file standing in for /dev/mem."""
    path = tmp_path / "mem"
    path.write_bytes(PATTERN)
    return str(path)


@pytest.fixture
async def make_window(backing):
    """Build and start windows on the backing file; stop them all
    at teardown."""
    windows = []

    async def make(base, size, path=backing):
        window = DevMemWindow(base, size, path=path)
        await window.start()
        windows.append(window)
        return window

    yield make
    for window in windows:
        await window.stop()


async def _root(backing):
    root = HwRoot()
    root.add_enumerator(DevMemEnumerator(path=backing))
    await root.ensure_started()
    return root


class TestParse:
    def test_hex(self):
        assert _parse_window("0x1000:0x100") == (0x1000, 0x100)

    def test_mixed_radix(self):
        assert _parse_window("4096:0b100") == (4096, 4)
        assert _parse_window("0o10:1_000") == (8, 1000)

    @pytest.mark.parametrize("name", [
        "0x1000", "0x1000:", ":0x10", "0x1000:0", "abc:def",
        "0x1000:0x10:1", "0x1000 :0x10", "",
    ])
    def test_rejects(self, name):
        with pytest.raises(ValueError):
            _parse_window(name)


class TestBroker:
    async def test_bad_name_is_no_match(self, backing):
        broker = DevMemBroker(path=backing)
        with pytest.raises(NoMatch):
            await broker.child_summon("nope")

    async def test_summons_started_window(self, backing):
        root = await _root(backing)
        window = await root.child_summon("devmem", "0x1000:0x100")
        assert isinstance(window, DevMemWindow)
        assert window.name == "0x1000:0x100"
        assert window.started
        assert window.metadata["base"] == 0x1000
        assert window.metadata["size"] == 0x100
        assert window.metadata["path"] == backing
        await root.stop_tree()


class TestMapping:
    async def test_unaligned_base(self, make_window):
        base = GRAN + 0x10
        window = await make_window(base, 0x30)
        assert await window.mem_read(0, 0x30) == PATTERN[base:base + 0x30]
        assert window.metadata["page_base"] == GRAN
        assert window.metadata["length"] == GRAN

    async def test_window_across_page_boundary(self, make_window):
        base = GRAN - 8
        window = await make_window(base, 16)
        assert await window.mem_read(0, 16) == PATTERN[base:base + 16]
        assert window.metadata["length"] == 2 * GRAN

    async def test_tail_of_mapping_is_unreachable(self, make_window):
        window = await make_window(0, 16)
        with pytest.raises(ValueError):
            await window.mem_read(16, 1)


class TestRegisterOps:
    BASE = GRAN + 0x40

    async def test_reads_are_little_endian(self, make_window):
        window = await make_window(self.BASE, 0x100)

        def expect(n):
            return int.from_bytes(PATTERN[self.BASE:self.BASE + n], "little")

        assert await window.read8(0) == expect(1)
        assert await window.read16(0) == expect(2)
        assert await window.read32(0) == expect(4)

    async def test_writes_reach_the_file(self, make_window, backing):
        window = await make_window(self.BASE, 0x100)
        await window.write32(4, 0xdeadbeef)
        await window.write16(8, 0x1234)
        await window.write8(10, 0x5a)
        with open(backing, "rb") as f:
            f.seek(self.BASE + 4)
            assert f.read(7) == bytes.fromhex("efbeadde34125a")
        assert await window.read32(4) == 0xdeadbeef

    @pytest.mark.parametrize("call", [
        lambda w: w.read16(1),
        lambda w: w.read32(1),
        lambda w: w.read32(2),
        lambda w: w.write32(2, 0),
        lambda w: w.write16(3, 0),
    ])
    async def test_unaligned_rejected(self, make_window, call):
        window = await make_window(self.BASE, 0x100)
        with pytest.raises(ValueError, match="unaligned"):
            await call(window)

    @pytest.mark.parametrize("call", [
        lambda w: w.read32(0x100 - 2),
        lambda w: w.read8(0x100),
        lambda w: w.read8(-1),
        lambda w: w.write32(0x100, 0),
    ])
    async def test_bounds_rejected(self, make_window, call):
        window = await make_window(self.BASE, 0x100)
        with pytest.raises(ValueError, match="outside"):
            await call(window)

    @pytest.mark.parametrize("call", [
        lambda w: w.write8(0, 0x100),
        lambda w: w.write16(0, 0x10000),
        lambda w: w.write32(0, 1 << 32),
        lambda w: w.write32(0, -1),
    ])
    async def test_oversized_value_rejected(self, make_window, call):
        window = await make_window(self.BASE, 0x100)
        with pytest.raises(ValueError, match="does not fit"):
            await call(window)


class TestBulkOps:
    async def test_round_trip(self, make_window):
        window = await make_window(0x20, 0x100)
        await window.mem_write(0x10, b"hello")
        assert await window.mem_read(0x10, 5) == b"hello"
        assert await window.mem_read(0x0f, 7) == (
            PATTERN[0x2f:0x30] + b"hello" + PATTERN[0x35:0x36])

    async def test_exact_end(self, make_window):
        window = await make_window(0x20, 0x100)
        assert await window.mem_read(0xfc, 4) == PATTERN[0x11c:0x120]
        await window.mem_write(0xfc, b"abcd")
        assert await window.mem_read(0xfc, 4) == b"abcd"

    async def test_past_end_rejected(self, make_window):
        window = await make_window(0x20, 0x100)
        with pytest.raises(ValueError):
            await window.mem_read(0xfc, 5)
        with pytest.raises(ValueError):
            await window.mem_write(0x100, b"x")

    async def test_empty_read(self, make_window):
        window = await make_window(0x20, 0x100)
        assert await window.mem_read(0x10, 0) == b""


class TestBatch:
    async def test_per_op_resolution(self, make_window):
        window = await make_window(0x20, 0x100)
        results = await asyncio.gather(
            window.read32(0), window.read32(1),
            window.mem_read(0, 8), window.read8(0x100),
            return_exceptions=True)
        assert results[0] == int.from_bytes(PATTERN[0x20:0x24], "little")
        assert isinstance(results[1], ValueError)
        assert results[2] == PATTERN[0x20:0x28]
        assert isinstance(results[3], ValueError)


class TestLifecycle:
    async def test_not_started_rejects(self, backing):
        window = DevMemWindow(0, 0x100, path=backing)
        with pytest.raises(RuntimeError):
            await window.read32(0)

    async def test_stop_then_access_rejects(self, backing):
        window = DevMemWindow(0, 0x100, path=backing)
        await window.start()
        await window.read32(0)
        await window.write32(0, 1)
        await window.stop()
        with pytest.raises(RuntimeError):
            await window.read32(0)

    async def test_start_is_idempotent(self, backing):
        window = DevMemWindow(0, 0x100, path=backing)
        await window.start()
        before = lifecycle().pending()
        await window.start()
        assert lifecycle().pending() == before
        await window.stop()

    async def test_lifecycle_registration(self, backing):
        window = DevMemWindow(0, 0x100, path=backing)
        baseline = lifecycle().pending()
        await window.start()
        assert lifecycle().pending() == baseline + 1
        await window.stop()
        assert lifecycle().pending() == baseline

    async def test_stop_without_start(self, backing):
        window = DevMemWindow(0, 0x100, path=backing)
        await window.stop()

    async def test_missing_path(self, tmp_path):
        window = DevMemWindow(0, 0x100, path=str(tmp_path / "missing"))
        baseline = lifecycle().pending()
        with pytest.raises(FileNotFoundError):
            await window.start()
        assert lifecycle().pending() == baseline


class TestEnumeration:
    async def test_populate_is_idempotent(self, backing):
        root = HwRoot()
        enumerator = DevMemEnumerator(path=backing)
        await enumerator.populate(root)
        await enumerator.populate(root)
        assert [c.name for c in root.children] == ["devmem"]

    def test_registered_as_standard_enumerator(self):
        from acrobe.adapter.model import (
            _import_standard_enumerators, enumerator_db)
        _import_standard_enumerators()
        assert DevMemEnumerator in enumerator_db.registry["devmem"]


class TestClientDispatch:
    async def test_client_summoned_on_window(self, backing):
        spawned = []

        def make_fake(bus, name):
            node = Node(f"{name}-on-{bus.name}")
            spawned.append((bus, node))
            return node

        memory.Interface.child_db.register("fake")(make_fake)
        try:
            root = await _root(backing)
            window = await root.child_summon("devmem", "0x1000:0x100")
            assert "fake" in window.child_hints()
            client = await root.child_summon(
                "devmem", "0x1000:0x100", "fake")
            assert spawned == [(window, client)]
            assert client.parent is window
            assert client.name == "fake-on-0x1000:0x100"
            with pytest.raises(NoMatch):
                await window.child_summon("unknown")
            await root.stop_tree()
        finally:
            memory.Interface.child_db.registry.pop("fake", None)
