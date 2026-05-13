"""Tests for the SEGGER RTT pipe."""

import asyncio
import struct
import pytest

from acrobe.component.arm.memory import BusRam
from acrobe.node import Node
from acrobe.target.memory import Memory
from acrobe.target.region import Ram
from acrobe.target.rtt import RTT_MAGIC, Rtt, RttError


# -- Mock bus simulating a Mem-AP -----------------------------------

class MockBus:
    """In-memory bus. mem_read/mem_write hit a sparse dict — the
    target stages the SEGGER control block + ring buffers here,
    and the test inspects writes the host issues back."""

    def __init__(self):
        self.memory = bytearray(0x60000)  # 384 KB scratch
        self.base = 0x20000000
        self.writes: list[tuple[int, bytes]] = []

    def __future(self, value):
        loop = asyncio.get_event_loop()
        f = loop.create_future()
        f.set_result(value)
        return f

    def __offset(self, addr):
        if addr < self.base or addr >= self.base + len(self.memory):
            raise IOError(f"out of range: 0x{addr:x}")
        return addr - self.base

    def mem_read(self, addr, size):
        off = self.__offset(addr)
        return self.__future(bytes(self.memory[off:off + size]))

    def mem_write(self, addr, data):
        off = self.__offset(addr)
        self.memory[off:off + len(data)] = data
        self.writes.append((addr, bytes(data)))
        return self.__future(None)

    def read32(self, addr):
        off = self.__offset(addr)
        return self.__future(
            int.from_bytes(bytes(self.memory[off:off + 4]), "little"))

    def write32(self, addr, data):
        off = self.__offset(addr)
        self.memory[off:off + 4] = data.to_bytes(4, "little")
        self.writes.append((addr, data.to_bytes(4, "little")))
        return self.__future(None)


def _stage_control_block(bus, *, cb_addr, max_up=1, max_down=1,
                          up_buf_addr=None, up_buf_size=64,
                          down_buf_addr=None, down_buf_size=64):
    """Write a minimal RTT control block + buffers into the mock RAM.

    Channels with max=0 are header-only — no descriptor written."""
    if up_buf_addr is None:
        up_buf_addr = cb_addr + 0x100
    if down_buf_addr is None:
        down_buf_addr = cb_addr + 0x200
    base = bus.base
    cb_off = cb_addr - base
    # Header
    bus.memory[cb_off:cb_off + 16] = RTT_MAGIC
    bus.memory[cb_off + 16:cb_off + 20] = struct.pack("<I", max_up)
    bus.memory[cb_off + 20:cb_off + 24] = struct.pack("<I", max_down)
    info = {
        "up_buf_addr": up_buf_addr, "up_buf_size": up_buf_size,
        "down_buf_addr": down_buf_addr, "down_buf_size": down_buf_size,
    }
    up_off = cb_off + 24
    if max_up > 0:
        bus.memory[up_off:up_off + 24] = struct.pack(
            "<6I", 0, up_buf_addr, up_buf_size, 0, 0, 0)
        info["up_desc_addr"] = base + up_off
    down_off = cb_off + 24 + max_up * 24
    if max_down > 0:
        bus.memory[down_off:down_off + 24] = struct.pack(
            "<6I", 0, down_buf_addr, down_buf_size, 0, 0, 0)
        info["down_desc_addr"] = base + down_off
    return info


def _wr_target_up(bus, ud, wroff, payload):
    """Target-side: write payload into UP ring at current WrOff,
    advance WrOff."""
    bus.memory[ud["up_buf_addr"] - bus.base + wroff:
               ud["up_buf_addr"] - bus.base + wroff + len(payload)] = payload
    new_wr = (wroff + len(payload)) % ud["up_buf_size"]
    bus.memory[ud["up_desc_addr"] - bus.base + 12:
               ud["up_desc_addr"] - bus.base + 16] = struct.pack("<I", new_wr)
    return new_wr


def _make_rtt(bus, *, cb_addr=None, up_buf_size=64, down_buf_size=64):
    """Build a working Rtt instance wired to MockBus + BusRam."""
    sram = BusRam("sram", bus.base, len(bus.memory), bus)
    Node._child_attach(Memory(bus), sram) if False else None
    # Attach sram under a Memory so the Rtt's parent ref chain has a bus.
    mem = Memory(bus)
    mem._child_attach(sram)
    rtt = Rtt(sram)
    if cb_addr is not None:
        rtt.cb_addr = cb_addr
    return rtt, sram


# -- Scan -----------------------------------------------------------

class TestScan:
    @pytest.mark.asyncio
    async def test_finds_magic(self):
        bus = MockBus()
        _stage_control_block(bus, cb_addr=0x20000400)
        rtt, _ = _make_rtt(bus)
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt.cb_addr == 0x20000400
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_finds_magic_at_low_offset(self):
        """Real-world: SEGGER tends to land the control block early
        in SRAM (e.g. 0x20000080 on nRF52). Verify low-offset hit."""
        bus = MockBus()
        _stage_control_block(bus, cb_addr=0x20000080)
        rtt, _ = _make_rtt(bus)
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt.cb_addr == 0x20000080
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_rescan_until_magic_appears(self):
        """If the firmware hasn't called SEGGER_RTT_Init yet, the
        scan retries until it does."""
        bus = MockBus()
        rtt, _ = _make_rtt(bus)
        rtt.RESCAN_INTERVAL = 0.05  # speed up the test
        await rtt.start()
        # First couple of poll rounds: nothing.
        await asyncio.sleep(0.1)
        assert not rtt._Rtt__ready.is_set()
        # Now "firmware" initialises RTT.
        _stage_control_block(bus, cb_addr=0x20000400)
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt.cb_addr == 0x20000400
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_skip_scan_when_address_given(self):
        bus = MockBus()
        _stage_control_block(bus, cb_addr=0x20001000)
        rtt, _ = _make_rtt(bus, cb_addr=0x20001000)
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt.cb_addr == 0x20001000
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_magic_at_odd_alignment_is_ignored(self):
        bus = MockBus()
        # Plant the magic at a non-4B aligned offset; scan must miss
        # and keep retrying without setting ready.
        bus.memory[0x402:0x402 + len(RTT_MAGIC)] = RTT_MAGIC
        rtt, _ = _make_rtt(bus)
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.sleep(0.2)
        assert not rtt._Rtt__ready.is_set()
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_crossing_chunk_boundary(self):
        bus = MockBus()
        # Place the magic so it straddles a SCAN_CHUNK boundary
        # (default 4096). 4090 → magic spans 4090-4105.
        addr = bus.base + 0x1000 - 6
        addr -= addr & 3  # align to 4
        _stage_control_block(bus, cb_addr=addr)
        rtt, _ = _make_rtt(bus)
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt.cb_addr == addr
        await rtt.stop()


# -- Descriptor resolution ------------------------------------------

class TestResolve:
    @pytest.mark.asyncio
    async def test_resolves_buffer_addresses(self):
        bus = MockBus()
        ud = _stage_control_block(
            bus, cb_addr=0x20000400,
            up_buf_size=128, down_buf_size=256)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt._Rtt__up_buf_size == 128
        assert rtt._Rtt__up_buf_addr == ud["up_buf_addr"]
        assert rtt._Rtt__down_buf_size == 256
        assert rtt._Rtt__down_buf_addr == ud["down_buf_addr"]
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_no_down_channel_accepted(self):
        """Firmware that only logs (MaxNumDownBuffers=0) must be
        accepted — UP works, write() raises if used."""
        bus = MockBus()
        ud = _stage_control_block(
            bus, cb_addr=0x20000400, max_up=1, max_down=0)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt._Rtt__up_buf_addr == ud["up_buf_addr"]
        assert rtt._Rtt__down_buf_size == 0
        with pytest.raises(RttError):
            await rtt.write(b"hello")
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_no_up_channel_accepted(self):
        """Symmetric: firmware that only takes commands
        (MaxNumUpBuffers=0) — DOWN works, read() raises if used."""
        bus = MockBus()
        ud = _stage_control_block(
            bus, cb_addr=0x20000400, max_up=0, max_down=1)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt._Rtt__down_buf_addr == ud["down_buf_addr"]
        assert rtt._Rtt__up_buf_size == 0
        with pytest.raises(RttError):
            await rtt.read(8)
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_channel_out_of_range(self):
        """An unreachable channel keeps the pump in the
        ESTABLISH loop — ready never sets, writes block, no
        crash."""
        bus = MockBus()
        _stage_control_block(bus, cb_addr=0x20000400, max_up=1)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.up_channel = 5
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.sleep(0.2)
        assert not rtt._Rtt__ready.is_set()
        await rtt.stop()


# -- UP pump --------------------------------------------------------

class TestValidation:
    """`__resolve_descriptors` refuses anything that doesn't look
    like a real RTT control block — protects against the user
    pointing `address=` at noise or a half-initialised block."""

    @pytest.mark.asyncio
    async def test_missing_magic(self):
        bus = MockBus()
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.RESCAN_INTERVAL = 0.05
        # No control block staged → all zero bytes at cb_addr.
        await rtt.start()
        await asyncio.sleep(0.2)
        assert not rtt._Rtt__ready.is_set()
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_absurd_max_buffers(self):
        bus = MockBus()
        # Stage a header with a bogus MaxNumUpBuffers.
        off = 0x400
        bus.memory[off:off + 16] = RTT_MAGIC
        bus.memory[off + 16:off + 20] = struct.pack("<I", 99)  # > sanity cap
        bus.memory[off + 20:off + 24] = struct.pack("<I", 1)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.sleep(0.2)
        assert not rtt._Rtt__ready.is_set()
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_buffer_pointer_outside_parent_ram(self):
        bus = MockBus()
        # Stage a normal header but point the UP buffer outside SRAM.
        ud = _stage_control_block(bus, cb_addr=0x20000400)
        # Rewrite up_buf_addr to something well past parent Ram end.
        up_desc_off = ud["up_desc_addr"] - bus.base
        bus.memory[up_desc_off + 4:up_desc_off + 8] = struct.pack(
            "<I", 0x40000000)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.sleep(0.2)
        assert not rtt._Rtt__ready.is_set()
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_buffer_size_absurd(self):
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400)
        # 1 MB buffer in 256 KB SRAM — both absurd-size and overflow.
        up_desc_off = ud["up_desc_addr"] - bus.base
        bus.memory[up_desc_off + 8:up_desc_off + 12] = struct.pack(
            "<I", 1 << 20)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.sleep(0.2)
        assert not rtt._Rtt__ready.is_set()
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_head_past_buffer_size(self):
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400, up_buf_size=64)
        # WrOff = 100, but size is 64 → invalid.
        up_desc_off = ud["up_desc_addr"] - bus.base
        bus.memory[up_desc_off + 12:up_desc_off + 16] = struct.pack(
            "<I", 100)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.sleep(0.2)
        assert not rtt._Rtt__ready.is_set()
        await rtt.stop()


class TestRevalidate:
    """Once the pump is running, magic disappearing → re-establish."""

    @pytest.mark.asyncio
    async def test_firmware_reload_triggers_reestablish(self):
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.poll_period = 0.005
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)

        # Simulate firmware wiping the control block.
        bus.memory[0x400:0x400 + 16] = b"\x00" * 16

        # On the next poll cycle the pump notices and clears ready.
        for _ in range(20):
            await asyncio.sleep(0.02)
            if not rtt._Rtt__ready.is_set():
                break
        assert not rtt._Rtt__ready.is_set()

        # Restage the control block — pump should re-establish.
        _stage_control_block(bus, cb_addr=0x20000400)
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_target_reset_in_place_triggers_reestablish(self):
        """`monitor reset; continue` leaves the magic alone but
        snaps WrOff/RdOff back to zero. The bare WrOff poll can't
        tell that from a normal advance — the full-descriptor
        check should catch it via RdOff drift."""
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400, up_buf_size=64)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.poll_period = 0.005
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)

        # Drain some bytes so our cached RdOff is non-zero.
        _wr_target_up(bus, ud, 0, b"before-reset")
        data = await asyncio.wait_for(rtt.read(len(b"before-reset")),
                                       timeout=1)
        assert data == b"before-reset"
        assert rtt._Rtt__up_buf_rdoff == len(b"before-reset")

        # Simulate target reset: WrOff and RdOff snap back to 0,
        # magic and the rest of the descriptor unchanged.
        desc_off = ud["up_desc_addr"] - bus.base
        bus.memory[desc_off + 12:desc_off + 16] = struct.pack("<I", 0)
        bus.memory[desc_off + 16:desc_off + 20] = struct.pack("<I", 0)

        # Pump must notice and re-establish (RdOff drift).
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not rtt._Rtt__ready.is_set():
                break
        # The re-establish loop will quickly re-resolve descriptors
        # — verify the pump came back ready and cached fresh state.
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        assert rtt._Rtt__up_buf_rdoff == 0

        # Fresh post-reset traffic should flow through.
        _wr_target_up(bus, ud, 0, b"after")
        data = await asyncio.wait_for(rtt.read(5), timeout=1)
        assert data == b"after"
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_down_descriptor_drift_triggers_reestablish(self):
        """If the target also reset the DOWN descriptor (WrOff
        moved without us writing), the per-cycle DOWN check
        must catch it."""
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.poll_period = 0.01
        rtt.RESCAN_INTERVAL = 0.05
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)

        # Stomp DOWN WrOff to a value we didn't write — only host
        # is supposed to touch WrOff on the DOWN buffer.
        down_desc_off = ud["down_desc_addr"] - bus.base
        bus.memory[down_desc_off + 12:down_desc_off + 16] = struct.pack(
            "<I", 42)

        for _ in range(30):
            await asyncio.sleep(0.02)
            if not rtt._Rtt__ready.is_set():
                break
        # After the drift, the pump should have re-established.
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        await rtt.stop()


class TestUpPump:
    @pytest.mark.asyncio
    async def test_reads_new_bytes(self):
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.poll_period = 0.005  # quick polling for the test
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)

        # Target writes "hello"
        _wr_target_up(bus, ud, 0, b"hello")
        data = await asyncio.wait_for(rtt.read(5), timeout=1)
        assert data == b"hello"
        # Host updated RdOff to 5.
        rdoff_addr = ud["up_desc_addr"] + 16
        rdoff_off = rdoff_addr - bus.base
        rdoff = struct.unpack("<I", bus.memory[rdoff_off:rdoff_off + 4])[0]
        assert rdoff == 5

        await rtt.stop()

    @pytest.mark.asyncio
    async def test_handles_wrap(self):
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400, up_buf_size=8)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.poll_period = 0.005
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)

        # Manually set RdOff to 6 (so available window wraps).
        bus.memory[ud["up_desc_addr"] - bus.base + 16:
                   ud["up_desc_addr"] - bus.base + 20] = struct.pack("<I", 6)
        rtt._Rtt__up_buf_rdoff = 6
        # Put bytes at positions 6,7,0,1 = "wrap"
        buf_off = ud["up_buf_addr"] - bus.base
        bus.memory[buf_off + 6:buf_off + 8] = b"wr"
        bus.memory[buf_off + 0:buf_off + 2] = b"ap"
        # Set target WrOff = 2 (so window is 6..2 mod 8 = "wrap")
        bus.memory[ud["up_desc_addr"] - bus.base + 12:
                   ud["up_desc_addr"] - bus.base + 16] = struct.pack("<I", 2)

        data = await asyncio.wait_for(rtt.read(4), timeout=1)
        assert data == b"wrap"
        await rtt.stop()


# -- DOWN write -----------------------------------------------------

class TestDownWrite:
    @pytest.mark.asyncio
    async def test_writes_into_ring(self):
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400, down_buf_size=32)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.poll_period = 0.005
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)
        await rtt.write(b"hello")
        # Bytes landed in the DOWN ring.
        buf_off = ud["down_buf_addr"] - bus.base
        assert bytes(bus.memory[buf_off:buf_off + 5]) == b"hello"
        # WrOff descriptor advanced to 5.
        wr_off = ud["down_desc_addr"] - bus.base + 12
        wroff = struct.unpack("<I", bus.memory[wr_off:wr_off + 4])[0]
        assert wroff == 5
        await rtt.stop()

    @pytest.mark.asyncio
    async def test_wraps_on_write(self):
        bus = MockBus()
        ud = _stage_control_block(bus, cb_addr=0x20000400, down_buf_size=8)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.poll_period = 0.005
        await rtt.start()
        await asyncio.wait_for(rtt._Rtt__ready.wait(), timeout=1)

        # Position WrOff at 7 (last slot) by faking that the target
        # consumed 7 bytes; RdOff = 7 so free_space = 7.
        bus.memory[ud["down_desc_addr"] - bus.base + 12:
                   ud["down_desc_addr"] - bus.base + 16] = struct.pack("<I", 7)
        bus.memory[ud["down_desc_addr"] - bus.base + 16:
                   ud["down_desc_addr"] - bus.base + 20] = struct.pack("<I", 7)
        rtt._Rtt__down_buf_wroff = 7

        await rtt.write(b"ab")
        # 'a' at position 7, 'b' wraps to 0.
        buf_off = ud["down_buf_addr"] - bus.base
        assert bus.memory[buf_off + 7] == ord("a")
        assert bus.memory[buf_off + 0] == ord("b")
        await rtt.stop()


# -- Registration / spawn ------------------------------------------

class TestRamSpawn:
    @pytest.mark.asyncio
    async def test_spawn_rtt_under_busram(self):
        """Confirms `child_spawn("rtt")` on a BusRam returns an Rtt."""
        bus = MockBus()
        _stage_control_block(bus, cb_addr=0x20000400)
        sram = BusRam("sram", bus.base, len(bus.memory), bus)
        mem = Memory(bus)
        mem._child_attach(sram)
        rtt = await sram.child_spawn("rtt")
        assert isinstance(rtt, Rtt)
        assert rtt.ram is sram
