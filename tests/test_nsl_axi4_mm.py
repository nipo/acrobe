"""nsl_axi4_mm: AXI4 master over the axi4_mm_on_stream datagram format.

The fake peer re-implements the RTL side (field layouts, channel
prefix, bursts, strobes, responses) against a byte-array memory,
without using the component's codec.
"""

import asyncio
from collections import deque

import pytest

from acrobe.component.nsl.axi4_mm import (
    AxiResponseError, Axi4MmOnStream, Axi4MmStreamCodec,
)
from acrobe.protocol import memory
from acrobe.protocol.datagram import Datagram, Recv, Send


class BitReader:
    """Pops little-endian bit fields, LSB first."""

    def __init__(self, data: bytes):
        self.value = int.from_bytes(data, "little")

    def take(self, width: int) -> int:
        v = self.value & ((1 << width) - 1)
        self.value >>= width
        return v


class FakeAxiPeer(Datagram):
    """``axi4_mm_on_stream`` + an AXI RAM, seen from the datagram side."""

    def __init__(self, *, addr_bits=32, data_bytes=4, len_bits=0,
                 id_bits=0, burst_field=False, size_field=False,
                 mem_size=0x4000):
        super().__init__("fake")
        self.addr_bits = addr_bits
        self.data_bytes = data_bytes
        self.len_bits = len_bits
        self.id_bits = id_bits
        self.burst_field = burst_field and len_bits != 0
        self.size_field = size_field
        self.mem = bytearray(mem_size)
        self.errors = []        # (lo, hi, resp, kind)
        self.silent = False
        self.refuse = False
        self.hold = False
        self.held = []
        self.events = []        # ("AW"/"AR"/"B"/"R", addr, beats, id)
        self.strobes = []       # per W beat
        self.__aw = deque()
        self.__rx = asyncio.Queue()

    # -- Datagram side --

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                self.handle(bytes(op.data))
                future.set_result(None)
            elif isinstance(op, Recv):
                asyncio.ensure_future(self.__deliver(future))

    async def __deliver(self, future):
        frame = await self.__rx.get()
        if future.done():
            return
        if isinstance(frame, BaseException):
            future.set_exception(frame)
        else:
            future.set_result((frame, None))

    def emit(self, frame):
        if self.silent:
            return
        if self.refuse:
            self.__rx.put_nowait(ConnectionRefusedError("refused"))
            return
        if self.hold:
            self.held.append(frame)
        else:
            self.__rx.put_nowait(frame)

    def release(self):
        self.hold = False
        for frame in self.held:
            self.__rx.put_nowait(frame)
        self.held = []

    # -- RTL side --

    def address_bytes(self):
        bits = (self.addr_bits + 3 + (3 if self.size_field else 0)
                + (2 if self.burst_field else 0) + self.len_bits
                + self.id_bits)
        return (bits + 7) // 8

    def parse_address(self, payload):
        assert len(payload) == self.address_bytes()
        r = BitReader(payload)
        addr = r.take(self.addr_bits)
        r.take(3)                            # prot
        size = r.take(3) if self.size_field else None
        burst = r.take(2) if self.burst_field else 1
        beats = r.take(self.len_bits) + 1
        axi_id = r.take(self.id_bits)
        assert r.value == 0
        assert burst == 1, "INCR expected"
        if size is not None:
            assert 1 << size == self.data_bytes
        assert addr % self.data_bytes == 0, "bursts start aligned"
        end = addr + beats * self.data_bytes
        assert addr // 4096 == (end - 1) // 4096, "4 KiB crossed"
        return addr, beats, axi_id

    def error_for(self, kind, addr, beats):
        end = addr + beats * self.data_bytes
        for lo, hi, resp, k in self.errors:
            if k == kind and addr < hi and lo < end:
                return resp
        return 0

    def handle(self, frame):
        channel, payload = frame[0], frame[1:]
        n = self.data_bytes
        if channel == 1:
            addr, beats, axi_id = self.parse_address(payload)
            self.events.append(("AW", addr, beats, axi_id))
            self.__aw.append((addr, beats, axi_id))
        elif channel == 4:
            addr, beats, axi_id = self.__aw.popleft()
            w_bytes = (9 * n + 7) // 8
            assert len(payload) == beats * w_bytes, "one frame per burst"
            resp = self.error_for("write", addr, beats)
            for beat in range(beats):
                r = BitReader(payload[beat * w_bytes:(beat + 1) * w_bytes])
                data = r.take(8 * n).to_bytes(n, "little")
                strb = r.take(n)
                self.strobes.append(strb)
                if resp:
                    continue
                for lane in range(n):
                    if strb >> lane & 1:
                        self.mem[(addr + beat * n + lane) % len(self.mem)] \
                            = data[lane]
            self.events.append(("B", addr, beats, axi_id))
            v = resp | axi_id << 2
            self.emit(bytes([0]) + v.to_bytes((2 + self.id_bits + 7) // 8,
                                              "little"))
        elif channel == 2:
            addr, beats, axi_id = self.parse_address(payload)
            self.events.append(("AR", addr, beats, axi_id))
            resp = self.error_for("read", addr, beats)
            r_bytes = (8 * n + 2 + self.id_bits + 7) // 8
            out = bytearray([3])
            for beat in range(beats):
                base = (addr + beat * n) % len(self.mem)
                data = int.from_bytes(self.mem[base:base + n], "little")
                v = data | resp << (8 * n) | axi_id << (8 * n + 2)
                out += v.to_bytes(r_bytes, "little")
            self.events.append(("R", addr, beats, axi_id))
            self.emit(bytes(out))
        else:
            raise AssertionError(f"host sent channel {channel}")


DEMO = "nsl_axi4_mm(id_width=2,max_length=16,burst=1)"


async def summon(peer, name=DEMO):
    return await peer.child_summon(name)


@pytest.fixture
async def demo():
    peer = FakeAxiPeer(len_bits=4, id_bits=2, burst_field=True)
    node = await summon(peer)
    yield peer, node
    await node.stop()


# -- codec against frames captured from the RTL --

class TestCodec:
    def codec(self):
        return Axi4MmStreamCodec(id_width=2, max_length=16, burst=True)

    def test_sizes(self):
        c = self.codec()
        assert (c.address_bits, c.address_bytes) == (43, 6)
        assert (c.w_bytes, c.b_bytes, c.r_bytes) == (5, 1, 5)

    def test_rtl_address_frame(self):
        # AW, 0x100, 2 beats, INCR, ID 1.
        frame = self.codec().address(1, 0x100, 2, 1, 0)
        assert frame == bytes.fromhex("01" "0001000028" "02")

    def test_rtl_write_frame(self):
        frame = self.codec().write_data(bytes.fromhex("1122334455667788"),
                                        [0xf, 0x5])
        assert frame == bytes.fromhex("04" "112233440f" "5566778805")

    def test_rtl_responses(self):
        c = self.codec()
        assert c.write_response(bytes.fromhex("04")) == (0, 1)
        beats = c.read_data(bytes.fromhex(
            "1122334408" "5500770008" "0000000008"))
        assert beats == [(bytes.fromhex("11223344"), 0, 2),
                         (bytes.fromhex("55007700"), 0, 2),
                         (bytes(4), 0, 2)]

    def test_optional_fields(self):
        c = Axi4MmStreamCodec(address_width=16, data_bus_width=64,
                              id_width=3, user_width=1, max_length=256,
                              size=True, burst=True, cache=True, lock=True,
                              qos=True, region=True)
        assert c.address_bits == 16 + 3 + 3 + 2 + 4 + 8 + 1 + 3 + 4 + 4 + 1
        assert c.w_bytes == (64 + 8 + 1 + 7) // 8
        v = int.from_bytes(c.address(2, 0x1238, 3, 5, 2)[1:], "little")
        assert v & 0xffff == 0x1238
        assert v >> 16 & 7 == 2          # prot
        assert v >> 19 & 7 == 3          # size: 8 bytes
        assert v >> 22 & 3 == 1          # INCR
        assert v >> 28 & 0xff == 2       # len
        assert v >> 37 & 7 == 5          # id

    def test_burst_field_absent_without_length(self):
        assert Axi4MmStreamCodec(burst=True).address_bits == 35

    def test_bursts(self):
        c = self.codec()
        assert [(b.addr, b.beats, b.offset) for b in c.bursts(0x0ffe, 70, 16)] \
            == [(0x0ffc, 1, -2), (0x1000, 16, 2), (0x1040, 1, 66)]

    @pytest.mark.parametrize("kwargs", [
        dict(data_bus_width=24), dict(data_bus_width=4),
        dict(max_length=12), dict(max_length=512), dict(address_width=0),
    ])
    def test_bad_config(self, kwargs):
        with pytest.raises(ValueError):
            Axi4MmStreamCodec(**kwargs)


# -- component against the fake peer --

async def test_registered():
    assert "nsl_axi4_mm" in Datagram.db.registry


async def test_aligned_roundtrip(demo):
    peer, node = demo
    data = bytes(range(64))
    await node.mem_write(0x200, data)
    assert peer.mem[0x200:0x240] == data
    assert await node.mem_read(0x200, 64) == data
    assert [e[:3] for e in peer.events] == [
        ("AW", 0x200, 16), ("B", 0x200, 16),
        ("AR", 0x200, 16), ("R", 0x200, 16)]


async def test_multi_burst(demo):
    peer, node = demo
    data = bytes((i * 7) & 0xff for i in range(300))
    await node.mem_write(0x100, data)
    assert await node.mem_read(0x100, 300) == data
    aw = [e for e in peer.events if e[0] == "AW"]
    assert [(a, b) for _, a, b, _ in aw] == [
        (0x100, 16), (0x140, 16), (0x180, 16), (0x1c0, 16), (0x200, 11)]


async def test_4k_boundary(demo):
    peer, node = demo
    data = bytes(range(32))
    await node.mem_write(0x0ff0, data)
    assert await node.mem_read(0x0ff0, 32) == data
    ar = [e for e in peer.events if e[0] == "AR"]
    assert [(a, b) for _, a, b, _ in ar] == [(0x0ff0, 4), (0x1000, 4)]


async def test_unaligned_write(demo):
    peer, node = demo
    peer.mem[0x300:0x320] = b"\xaa" * 32
    await node.mem_write(0x303, b"\x01\x02\x03\x04\x05\x06")
    assert peer.mem[0x300:0x30c] == bytes.fromhex("aaaaaa010203040506aaaaaa")
    assert peer.strobes == [0x8, 0xf, 0x1]


async def test_unaligned_read(demo):
    peer, node = demo
    peer.mem[0x400:0x410] = bytes(range(16))
    assert await node.mem_read(0x401, 13) == bytes(range(1, 14))
    assert await node.mem_read(0x406, 1) == b"\x06"


async def test_register_access(demo):
    peer, node = demo
    await node.write32(0x500, 0x12345678)
    await node.write8(0x506, 0x9a)
    assert peer.mem[0x500:0x508] == bytes.fromhex("785634120000" "9a00")
    assert await node.read16(0x502) == 0x1234
    assert await node.read32(0x500) == 0x12345678


async def test_read_after_write_waits(demo):
    peer, node = demo
    w = node.mem_write(0x600, b"\x55" * 8)
    r = node.mem_read(0x600, 8)
    w2 = node.mem_write(0x600, b"\x66" * 8)
    await asyncio.gather(w, r, w2)
    assert r.result() == b"\x55" * 8
    assert [e[0] for e in peer.events] == ["AW", "B", "AR", "R", "AW", "B"]


async def test_batch_ids(demo):
    peer, node = demo
    await node.mem_write(0x0, bytes(4))
    assert {e[3] for e in peer.events} == {0}

    peer2 = FakeAxiPeer(len_bits=4, id_bits=2, burst_field=True)
    node2 = await summon(peer2, DEMO[:-1] + ",id=3)")
    await node2.mem_read(0x0, 4)
    assert {e[3] for e in peer2.events} == {3}
    await node2.stop()


async def test_window():
    peer = FakeAxiPeer(len_bits=2)
    node = await summon(peer, "nsl_axi4_mm(max_length=4,window=3)")
    peer.hold = True
    task = asyncio.ensure_future(node.mem_read(0x0, 4 * 4 * 5))
    for _ in range(10):
        await asyncio.sleep(0)
    assert len([e for e in peer.events if e[0] == "AR"]) == 3
    peer.release()
    assert await task == bytes(80)
    assert len([e for e in peer.events if e[0] == "AR"]) == 5
    await node.stop()


async def test_max_burst():
    peer = FakeAxiPeer(len_bits=8, burst_field=True)
    node = await summon(
        peer, "nsl_axi4_mm(max_length=256,burst=true,max_burst=4)")
    await node.mem_write(0x0, bytes(40))
    assert [e[2] for e in peer.events if e[0] == "AW"] == [4, 4, 2]
    await node.stop()


async def test_write_error_isolated(demo):
    peer, node = demo
    peer.errors.append((0x800, 0x804, 2, "write"))
    bad = node.mem_write(0x7f0, bytes(32))
    good = node.mem_write(0x900, b"\x11" * 4)
    with pytest.raises(AxiResponseError) as info:
        await bad
    assert info.value.resp == 2 and info.value.kind == "write"
    await good
    assert peer.mem[0x900:0x904] == b"\x11" * 4


async def test_read_decerr(demo):
    peer, node = demo
    peer.errors.append((0x1000, 0x2000, 3, "read"))
    with pytest.raises(AxiResponseError, match="DECERR"):
        await node.mem_read(0x0ffc, 8)
    assert await node.mem_read(0x0ff8, 4) == bytes(4)


async def test_timeout_moves_id():
    peer = FakeAxiPeer(len_bits=4, id_bits=2, burst_field=True)
    node = await summon(peer, DEMO[:-1] + ",timeout=0.05)")
    peer.hold = True
    first = node.mem_read(0x0, 4)
    second = node.mem_read(0x10, 4)
    with pytest.raises(TimeoutError):
        await first
    with pytest.raises(TimeoutError):
        await second
    # The late response carries the old ID and must not answer the
    # next transaction.
    peer.mem[0x20:0x24] = b"\x01\x02\x03\x04"
    peer.release()
    assert await node.mem_read(0x20, 4) == b"\x01\x02\x03\x04"
    assert [e[3] for e in peer.events if e[0] == "AR"] == [0, 0, 1]
    await node.stop()


async def test_transport_error_aborts_batch(demo):
    peer, node = demo
    peer.refuse = True
    first = node.mem_write(0x0, bytes(4))
    second = node.mem_read(0x0, 4)
    with pytest.raises(ConnectionRefusedError):
        await first
    with pytest.raises(ConnectionRefusedError):
        await second
    peer.refuse = False
    await node.mem_write(0x0, b"\x01\x02\x03\x04")
    assert await node.mem_read(0x0, 4) == b"\x01\x02\x03\x04"


async def test_out_of_address_space():
    peer = FakeAxiPeer(addr_bits=12)
    node = await summon(peer, "nsl_axi4_mm(address_width=12)")
    bad = node.mem_read(0xffe, 4)
    good = node.mem_write(0xffc, b"\x01\x02\x03\x04")
    with pytest.raises(ValueError, match="12-bit"):
        await bad
    await good
    assert peer.mem[0xffc:0x1000] == b"\x01\x02\x03\x04"
    await node.stop()


async def test_empty_ops(demo):
    peer, node = demo
    assert await node.mem_read(0x0, 0) == b""
    await node.mem_write(0x0, b"")
    assert peer.events == []


async def test_no_wait_ops(demo):
    peer, node = demo
    node.post_no_wait(memory.WriteBlob(0x40, b"\x42" * 4))
    assert await node.mem_read(0x40, 4) == b"\x42" * 4


@pytest.mark.parametrize("opts", [
    "max_length=4,max_burst=8", "id=4,id_width=2", "prot=8",
    "window=0", "timeout=0", "data_bus_width=12",
])
async def test_bad_options(opts):
    peer = FakeAxiPeer()
    with pytest.raises(ValueError):
        await summon(peer, f"nsl_axi4_mm({opts})")
