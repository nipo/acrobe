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
    """Write a minimal RTT control block + buffers into the mock RAM."""
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
    # UP[0] descriptor
    up_off = cb_off + 24
    bus.memory[up_off:up_off + 24] = struct.pack(
        "<6I", 0, up_buf_addr, up_buf_size, 0, 0, 0)
    # DOWN[0] descriptor
    down_off = cb_off + 24 + max_up * 24
    bus.memory[down_off:down_off + 24] = struct.pack(
        "<6I", 0, down_buf_addr, down_buf_size, 0, 0, 0)
    return {
        "up_buf_addr": up_buf_addr, "up_buf_size": up_buf_size,
        "down_buf_addr": down_buf_addr, "down_buf_size": down_buf_size,
        "up_desc_addr": base + up_off,
        "down_desc_addr": base + down_off,
    }


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
    async def test_channel_out_of_range(self):
        bus = MockBus()
        _stage_control_block(bus, cb_addr=0x20000400, max_up=1)
        rtt, _ = _make_rtt(bus, cb_addr=0x20000400)
        rtt.up_channel = 5
        await rtt.start()
        # Pump task fails on the bad channel; the failure surfaces
        # when we await it.
        with pytest.raises(RttError):
            await rtt._Rtt__pump_task


# -- UP pump --------------------------------------------------------

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
