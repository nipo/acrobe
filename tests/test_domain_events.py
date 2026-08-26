"""Tests for the first real domain users of the event bus
(Slice 4): program events from Loadable.write, mutate event from
Chain.tlr_and_refresh, reset event from Cortex-M Core.reset.
"""

import pytest

from acrobe.bitstring import BitString
from acrobe.event import Event, Phase, get_bus, reset_for_tests
from acrobe.memory_map import MemoryMap
from acrobe.node import Node
from acrobe.protocol.jtag import Chain, JtagInterface, Shift
from acrobe.target.loadable import Loadable
from acrobe.target.region import Region


@pytest.fixture(autouse=True)
def isolated_bus():
    reset_for_tests()
    yield
    reset_for_tests()


# ----- Loadable.write program events -----

class FakeRegion(Region):
    """Region that records writes to a bytearray. Plain
    pass-through plan_update (one chunk per (offset, data))."""

    def __init__(self, name, address, size):
        super().__init__(name, address, size)
        self.storage = bytearray(size)
        self.writes: list[tuple[int, bytes]] = []

    async def read(self, offset, size):
        return bytes(self.storage[offset:offset + size])

    async def write(self, offset, data):
        self.storage[offset:offset + len(data)] = data
        self.writes.append((offset, bytes(data)))


class FailingRegion(Region):
    """Region whose write always raises — for failure-path tests."""

    def __init__(self, name, address, size):
        super().__init__(name, address, size)

    async def read(self, offset, size):
        return bytes(size)

    async def write(self, offset, data):
        raise RuntimeError("write boom")


def make_target_with_loadable(*regions, target_name="MyTarget",
                              loadable_name="main"):
    target = Node(target_name)
    loadable = Loadable(loadable_name)
    target.child_add(loadable)
    for r in regions:
        loadable.child_add(r)
    return target, loadable


class TestProgramEvents:
    @pytest.mark.asyncio
    async def test_write_emits_pre_then_post(self):
        region = FakeRegion("flash", 0x1000, 0x100)
        target, loadable = make_target_with_loadable(region)
        await target.start_tree()

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="program")

        m = MemoryMap()
        m.append(0x1000, b"\xaa" * 32)
        await loadable.write(m)

        phases = [(e.action, e.phase) for e in events]
        # progress phase may fire between pre and post.
        assert phases[0] == ("program", Phase.PRE)
        assert phases[-1] == ("program", Phase.POST)
        # POST carries success=True.
        assert events[-1].properties["success"] is True

    @pytest.mark.asyncio
    async def test_write_carries_target_property(self):
        region = FakeRegion("flash", 0x1000, 0x100)
        target, loadable = make_target_with_loadable(
            region, target_name="MyChip")
        await target.start_tree()

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="program", phase=Phase.PRE)

        m = MemoryMap()
        m.append(0x1000, b"\x55" * 16)
        await loadable.write(m, do_erase=True, do_verify=False)

        assert events[0].properties["target"] == "MyChip"
        assert events[0].properties["do_erase"] is True
        assert events[0].properties["do_verify"] is False

    @pytest.mark.asyncio
    async def test_write_source_is_loadable_path(self):
        region = FakeRegion("flash", 0x1000, 0x100)
        target, loadable = make_target_with_loadable(
            region, target_name="t", loadable_name="main")
        await target.start_tree()

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="program", phase=Phase.POST)

        m = MemoryMap()
        m.append(0x1000, b"\x00" * 8)
        await loadable.write(m)

        assert events[0].source == "t/main"

    @pytest.mark.asyncio
    async def test_write_emits_progress_per_region(self):
        # Two non-empty regions → two progress events with the
        # region path and the byte count.
        r1 = FakeRegion("r1", 0x1000, 0x100)
        r2 = FakeRegion("r2", 0x2000, 0x100)
        target, loadable = make_target_with_loadable(r1, r2)
        await target.start_tree()

        progress_events: list[Event] = []
        get_bus().subscribe(lambda e: progress_events.append(e),
                            action="program", phase=Phase.PROGRESS)

        m = MemoryMap()
        m.append(0x1000, b"\xaa" * 16)
        m.append(0x2000, b"\xbb" * 32)
        await loadable.write(m)

        regions_seen = sorted(e.properties["region"] for e in progress_events)
        assert regions_seen == ["MyTarget/main/r1", "MyTarget/main/r2"]
        # Each progress carries written + total + target.
        for e in progress_events:
            assert "written" in e.properties
            assert "total" in e.properties
            assert e.properties["target"] == "MyTarget"

    @pytest.mark.asyncio
    async def test_write_failure_emits_post_with_error(self):
        region = FailingRegion("bad", 0x1000, 0x100)
        target, loadable = make_target_with_loadable(region)
        await target.start_tree()

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="program", phase=Phase.POST)

        m = MemoryMap()
        m.append(0x1000, b"\xff" * 8)
        with pytest.raises(RuntimeError, match="write boom"):
            await loadable.write(m)

        assert events[0].properties["success"] is False
        assert events[0].properties["error_class"] == "RuntimeError"


# ----- Chain.tlr_and_refresh mutate event -----

def _make_scripted_iface(scripts):
    """Iface that returns scripted Shift TDO values, then None
    for any leftover read."""
    class ScriptedIface(JtagInterface):
        def __init__(self):
            super().__init__(name="scripted")
            self.scripts = list(scripts)

        async def flush_ops(self, batch):
            for op, future in batch:
                if isinstance(op, Shift):
                    if op.read_tdo:
                        tdo = (self.scripts.pop(0)
                               if self.scripts
                               else BitString(0, len(op.tdi)))
                        future.set_result(tdo)
                    else:
                        future.set_result(None)
                else:
                    future.set_result(None)
    return ScriptedIface()


def _build_reset_dr(idcodes_with_irlens):
    out = BitString(0, 0)
    for idcode, _ in idcodes_with_irlens:
        if idcode is None:
            out = out + BitString(0, 1)
        else:
            out = out + BitString(idcode, 32)
    return out


def _build_capture_ir(irlens):
    out = BitString(0, 0)
    for irlen in irlens:
        seg = BitString(0b01, irlen) if irlen >= 2 else BitString(0, irlen)
        out = out + seg
    return out


def _shift_discover_payload(register: BitString):
    max_length = 512
    marker = 0xc05a5a03
    total = 32 + max_length + 4
    v = int(register)
    v |= marker << len(register)
    tdo = BitString(v, total)
    return tdo[:max_length + 32]


def _make_chain(iface):
    chain = Chain()
    iface.child_add(chain)
    return chain


class TestChainMutateEvent:
    @pytest.mark.asyncio
    async def test_mutate_emitted_when_chain_unchanged(self):
        chain_layout = [(0x11111111, 4), (0x22222223, 5)]
        reset_dr = _build_reset_dr(chain_layout)
        captured_ir = _build_capture_ir([il for _, il in chain_layout])
        bypass_dr = BitString(0, len(chain_layout))
        scripts = [
            _shift_discover_payload(reset_dr),
            _shift_discover_payload(captured_ir),
            _shift_discover_payload(bypass_dr),
        ]
        iface = _make_scripted_iface(scripts)
        chain = _make_chain(iface)
        chain.tap_add(0x11111111, irlen=4)
        chain.tap_add(0x22222223, irlen=5)

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="mutate")

        await chain.tlr_and_refresh()

        assert len(events) == 1
        evt = events[0]
        assert evt.action == "mutate"
        assert evt.phase == Phase.POST
        assert evt.properties["committed"] is True
        assert evt.properties["changed"] is False
        assert evt.properties["tap_count"] == 2

    @pytest.mark.asyncio
    async def test_mutate_changed_true_when_chain_shrinks(self):
        # Existing chain has two TAPs; refresh probe sees only one.
        # The unmatched TAP gets detached → before != after.
        old_layout = [(0x11111111, 4), (0x22222223, 5)]
        new_layout = [(0x11111111, 4)]
        reset_dr = _build_reset_dr(new_layout)
        captured_ir = _build_capture_ir([il for _, il in new_layout])
        bypass_dr = BitString(0, len(new_layout))
        scripts = [
            _shift_discover_payload(reset_dr),
            _shift_discover_payload(captured_ir),
            _shift_discover_payload(bypass_dr),
        ]
        iface = _make_scripted_iface(scripts)
        chain = _make_chain(iface)
        for idcode, irlen in old_layout:
            chain.tap_add(idcode, irlen=irlen)

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="mutate")

        await chain.tlr_and_refresh()

        assert len(events) == 1
        assert events[0].properties["changed"] is True
        assert events[0].properties["committed"] is True


# ----- Cortex-M Core.reset event -----

class _MockScs:
    """Just enough surface for Core.reset to run without hardware."""

    def __init__(self):
        self.reset_catch_calls: list[bool] = []
        self.cpu_reset_calls = 0

    async def set_reset_catch(self, on: bool):
        self.reset_catch_calls.append(on)

    async def cpu_reset(self):
        self.cpu_reset_calls += 1


def _make_cortex_core(name="cm0"):
    """Build a CortexMCore with the minimum to call reset()."""
    from acrobe.target.arm.cortex_m import CortexMCore
    # CortexMCore needs an SCS reference; we can construct it via
    # __new__ + manual init to avoid pulling the whole DP/ROM/AP
    # synthetic-tree machinery in for this small test.
    core = CortexMCore.__new__(CortexMCore)
    Node.__init__(core, name)
    core.scs = _MockScs()
    return core


class TestCortexResetEvent:
    @pytest.mark.asyncio
    async def test_reset_emits_pre_then_post(self):
        core = _make_cortex_core()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="reset")

        await core.reset(stop=True)

        phases = [(e.action, e.phase) for e in events]
        assert phases == [("reset", Phase.PRE), ("reset", Phase.POST)]
        # `kind="cpu"` carried in both, `stop=True` likewise.
        for e in events:
            assert e.properties["kind"] == "cpu"
            assert e.properties["stop"] is True
        assert events[1].properties["success"] is True

    @pytest.mark.asyncio
    async def test_reset_stop_false_property(self):
        core = _make_cortex_core()
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="reset", phase=Phase.PRE)

        await core.reset(stop=False)

        assert events[0].properties["stop"] is False

    @pytest.mark.asyncio
    async def test_reset_source_is_core_path(self):
        core = _make_cortex_core(name="core0")
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="reset")

        await core.reset(stop=True)

        # Standalone core — path is just its name.
        assert events[0].source == "core0"
