"""nsl_apb: APB master over the apb_stream_bridge datagram format.

The fake peer re-implements the RTL side (opcode, address and count
fields, word collection, status byte, malformed frames) byte by byte
against a word-addressed APB slave model, without using the
component's codec.
"""

import asyncio

import pytest

from acrobe.component.nsl.apb import (
    ApbError, ApbOnStream, ApbProtocolError, ApbStreamCodec,
)
from acrobe.protocol import memory
from acrobe.protocol.datagram import Datagram, Recv, Send


class FakeApbSlave:
    """APB slave: a byte array of whole words, PSLVERR on some ranges,
    every access logged."""

    def __init__(self, data_bytes, size=0x1000):
        self.data_bytes = data_bytes
        self.mem = bytearray(size)
        self.errors = []        # (lo, hi, kind)
        self.log = []           # ("R"/"W", addr)

    def error(self, kind, addr):
        return any(k == kind and lo <= addr < hi
                   for lo, hi, k in self.errors)

    def read(self, addr):
        self.log.append(("R", addr))
        base = addr % len(self.mem)
        word = bytes(self.mem[base:base + self.data_bytes])
        return word, self.error("read", addr)

    def write(self, addr, word):
        self.log.append(("W", addr))
        if self.error("write", addr):
            return True
        base = addr % len(self.mem)
        self.mem[base:base + self.data_bytes] = word
        return False


class FakeBridge(Datagram):
    """``apb_stream_bridge`` + an APB slave, seen from the datagram
    side."""

    def __init__(self, *, addr_bits=16, data_bytes=4, burst_l2=4,
                 identify=b"apb_ram"):
        super().__init__("fake")
        self.addr_bytes = (addr_bits + 7) // 8
        self.addr_mask = (1 << addr_bits) - 1
        self.data_bytes = data_bytes
        self.count_bytes = max(1, (burst_l2 + 7) // 8)
        self.count_mask = (1 << max(1, burst_l2)) - 1
        self.identify = identify
        self.slave = FakeApbSlave(data_bytes)
        self.frames = []
        self.silent = False
        self.refuse = False
        self.hold = False
        self.held = []
        self.__rx = asyncio.Queue()

    # -- Datagram side --

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                self.frames.append(bytes(op.data))
                self.emit(self.handle(bytes(op.data)))
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

    @staticmethod
    def field(data, offset, size):
        """Little-endian field, or None when the frame ends inside it."""
        if offset + size > len(data):
            return None
        return int.from_bytes(data[offset:offset + size], "little")

    def handle(self, frame):
        opcode = frame[0]
        if opcode == 0xff:
            return self.identify + b"\x00"
        if opcode not in (0x00, 0x80):
            return b"\x01"
        addr = self.field(frame, 1, self.addr_bytes)
        if addr is None:
            return b"\x01"
        body = frame[1 + self.addr_bytes:]
        err = False
        if opcode == 0x80:
            count = self.field(body, 0, self.count_bytes)
            if count is None:
                return b"\x01"
            out = bytearray()
            for _ in range((count & self.count_mask) + 1):
                word, e = self.slave.read(addr)
                out += word
                err |= e
                addr = (addr + self.data_bytes) & self.addr_mask
            return bytes(out) + bytes([err])
        n = self.data_bytes
        for i in range(len(body) // n):
            err |= self.slave.write(addr, body[i * n:(i + 1) * n])
            addr = (addr + n) & self.addr_mask
        if len(body) % n:
            err = True
        return bytes([err])


DEMO = "nsl_apb(address_width=16,data_bus_width=32,burst_length_l2=4)"


async def summon(peer, name=DEMO):
    return await peer.child_summon(name)


@pytest.fixture
async def demo():
    peer = FakeBridge()
    node = await summon(peer)
    yield peer, node
    await node.stop()


def with_options(extra):
    return DEMO[:-1] + "," + extra + ")"


# -- codec against frames captured from the RTL --

class TestCodec:
    def codec(self):
        return ApbStreamCodec(address_width=16, data_bus_width=32,
                              burst_length_l2=4)

    def test_rtl_commands(self):
        c = self.codec()
        assert c.identify() == bytes.fromhex("ff")
        assert c.read(0x40, 2) == bytes.fromhex("80400001")
        assert c.write(0x40, bytes.fromhex("0011223344556677")) \
            == bytes.fromhex("0040000011223344556677")

    def test_rtl_answers(self):
        assert ApbStreamCodec.answer(bytes.fromhex("6170625f72616d00")) \
            == (b"apb_ram", 0)
        assert ApbStreamCodec.answer(bytes.fromhex("001122334455667700")) \
            == (bytes.fromhex("0011223344556677"), 0)
        # Write ending mid-word, and a read frame ending in its address.
        assert ApbStreamCodec.answer(b"\x01") == (b"", 1)

    def test_fake_matches_rtl(self):
        # The fake bridge answers the captured exchanges like the RTL.
        bridge = FakeBridge()
        for command, answer in [
                ("ff00", "6170625f72616d00"),
                ("ff", "6170625f72616d00"),
                ("0040000011223344556677", "00"),
                ("80400001", "001122334455667700"),
                ("80400000", "0011223300"),
                ("0040001122", "01"),
                ("8040", "01"), ("80", "01"), ("00", "01"),
                ("0040", "01"), ("004000", "00"),
                ("42", "01"), ("420102", "01"),
                ("80fc0f00", "0000000000"),
        ]:
            assert bridge.handle(bytes.fromhex(command)).hex() == answer, \
                command
        assert bridge.handle(bytes.fromhex("8040000faa")) \
            == bytes.fromhex("0011223344556677") + bytes(57)

    def test_field_sizes(self):
        c = ApbStreamCodec(address_width=9, data_bus_width=8,
                           burst_length_l2=12)
        assert (c.address_bytes, c.count_bytes, c.data_bytes) == (2, 2, 1)
        assert c.read(0x1ff, 4096) == bytes.fromhex("80ff01ff0f")
        c = ApbStreamCodec(address_width=32, data_bus_width=16,
                           burst_length_l2=0)
        assert (c.address_bytes, c.count_bytes, c.max_read) == (4, 1, 1)
        assert c.read(0x12345678, 1) == bytes.fromhex("807856341200")
        with pytest.raises(ValueError):
            c.read(0, 2)

    def test_split(self):
        c = self.codec()
        assert [(r.addr, r.words, r.offset) for r in c.reads(0x3e, 70)] \
            == [(0x3c, 16, -2), (0x7c, 2, 62)]
        assert [(w.addr, w.words, w.offset)
                for w in c.writes(0x40, 72, 8)] \
            == [(0x40, 8, 0), (0x60, 8, 32), (0x80, 2, 64)]
        with pytest.raises(ValueError):
            c.writes(0x42, 4, 8)
        with pytest.raises(ValueError):
            c.writes(0x40, 6, 8)

    @pytest.mark.parametrize("kwargs", [
        dict(address_width=0), dict(address_width=33),
        dict(data_bus_width=64), dict(data_bus_width=24),
        dict(burst_length_l2=17),
    ])
    def test_bad_config(self, kwargs):
        args = dict(address_width=16, data_bus_width=32, burst_length_l2=4)
        with pytest.raises(ValueError):
            ApbStreamCodec(**{**args, **kwargs})


# -- component against the fake peer --

async def test_registered():
    assert "nsl_apb" in Datagram.db.registry


async def test_identify(demo):
    peer, node = demo
    assert await node.identify() == b"apb_ram"
    assert peer.frames == [b"\xff"]


async def test_identify_error():
    peer = FakeBridge()
    peer.handle = lambda frame: b"xx\x01"
    node = await summon(peer)
    with pytest.raises(ApbError):
        await node.identify()
    await node.stop()


async def test_roundtrip(demo):
    peer, node = demo
    data = bytes(range(64))
    await node.mem_write(0x100, data)
    assert peer.slave.mem[0x100:0x140] == data
    assert await node.mem_read(0x100, 64) == data
    assert peer.frames == [
        b"\x00\x00\x01" + data,
        b"\x80\x00\x01\x0f",
    ]


async def test_long_transfers_split(demo):
    peer, node = demo
    data = bytes((i * 7) & 0xff for i in range(200))
    await node.mem_write(0x200, data)
    assert await node.mem_read(0x200, 200) == data
    assert [(f[0], len(f)) for f in peer.frames] == [
        (0x00, 3 + 64), (0x00, 3 + 64), (0x00, 3 + 64), (0x00, 3 + 8),
        (0x80, 4), (0x80, 4), (0x80, 4), (0x80, 4)]
    assert [f[3] for f in peer.frames[4:]] == [15, 15, 15, 1]


async def test_max_write():
    peer = FakeBridge()
    node = await summon(peer, with_options("max_write=3"))
    await node.mem_write(0x0, bytes(32))
    assert [len(f) for f in peer.frames] == [15, 15, 11]
    await node.stop()


async def test_unaligned_read(demo):
    peer, node = demo
    peer.slave.mem[0x400:0x410] = bytes(range(16))
    assert await node.mem_read(0x401, 13) == bytes(range(1, 14))
    assert await node.read8(0x406) == 6
    assert await node.read16(0x402) == 0x0302
    assert peer.frames[1:] == [b"\x80\x04\x04\x00", b"\x80\x00\x04\x00"]


async def test_register_access(demo):
    peer, node = demo
    await node.write32(0x500, 0x12345678)
    assert peer.slave.mem[0x500:0x504] == bytes.fromhex("78563412")
    assert await node.read32(0x500) == 0x12345678
    assert peer.slave.log == [("W", 0x500), ("R", 0x500)]


async def test_partial_write_refused(demo):
    peer, node = demo
    for access in (node.write8(0x500, 1), node.write16(0x500, 1),
                   node.write32(0x502, 1), node.mem_write(0x500, bytes(6))):
        with pytest.raises(ValueError, match="whole 4-byte words"):
            await access
    assert peer.frames == []


async def test_byte_bus():
    peer = FakeBridge(addr_bits=12, data_bytes=1, burst_l2=2)
    node = await summon(
        peer, "nsl_apb(address_width=12,data_bus_width=8,burst_length_l2=2)")
    await node.write8(0x123, 0x5a)
    await node.mem_write(0x7, b"abcdef")
    assert await node.read8(0x123) == 0x5a
    assert await node.mem_read(0x7, 6) == b"abcdef"
    assert peer.frames[0] == b"\x00\x23\x01\x5a"
    await node.stop()


async def test_batch_order(demo):
    peer, node = demo
    w = node.mem_write(0x600, b"\x55" * 8)
    r = node.mem_read(0x600, 8)
    w2 = node.mem_write(0x600, b"\x66" * 8)
    r2 = node.mem_read(0x600, 8)
    await asyncio.gather(w, r, w2, r2)
    assert r.result() == b"\x55" * 8
    assert r2.result() == b"\x66" * 8
    assert [f[0] for f in peer.frames] == [0x00, 0x80, 0x00, 0x80]


async def test_one_batch_many_in_flight():
    peer = FakeBridge()
    node = await summon(peer, with_options("window=3"))
    peer.hold = True
    reads = [node.read32(4 * i) for i in range(5)]
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(peer.frames) == 3
    peer.release()
    peer.hold = False
    assert await asyncio.gather(*reads) == [0] * 5
    assert len(peer.frames) == 5
    await node.stop()


async def test_read_error_isolated(demo):
    peer, node = demo
    peer.slave.errors.append((0x80, 0x84, "read"))
    peer.slave.mem[0x40:0x44] = b"\x01\x02\x03\x04"
    bad = node.mem_read(0x40, 128)
    good = node.read32(0x40)
    with pytest.raises(ApbError) as info:
        await bad
    assert info.value.kind == "read" and info.value.addr == 0x80
    assert info.value.status == 1
    assert await good == 0x04030201


async def test_write_error(demo):
    peer, node = demo
    peer.slave.errors.append((0x44, 0x48, "write"))
    with pytest.raises(ApbError, match="write of 4 word"):
        await node.mem_write(0x40, b"\x11" * 16)
    # The bridge goes on with the other words.
    assert peer.slave.mem[0x40:0x50] == b"\x11" * 4 + bytes(4) + b"\x11" * 8
    await node.write32(0x80, 1)


async def test_short_answer_is_protocol_error(demo):
    peer, node = demo
    peer.handle = lambda frame: b"\x00\x00"
    with pytest.raises(ApbProtocolError, match="1 bytes answered, 4"):
        await node.read32(0)


async def test_timeout_keeps_order():
    peer = FakeBridge()
    node = await summon(peer, with_options("timeout=0.05"))
    peer.slave.mem[0x10:0x14] = b"\xaa" * 4
    peer.slave.mem[0x20:0x24] = b"\x01\x02\x03\x04"
    peer.hold = True
    first = node.read32(0x10)
    second = node.read32(0x14)
    with pytest.raises(TimeoutError):
        await first
    with pytest.raises(TimeoutError):
        await second
    # The two late answers must not answer the next command.
    peer.release()
    assert await node.mem_read(0x20, 4) == b"\x01\x02\x03\x04"
    await node.stop()


async def test_timeout_fails_rest_of_batch():
    peer = FakeBridge()
    node = await summon(peer, with_options("timeout=0.05,window=1"))
    peer.silent = True
    first = node.read32(0x0)
    second = node.write32(0x4, 1)
    with pytest.raises(TimeoutError):
        await first
    with pytest.raises(TimeoutError):
        await second
    assert len(peer.frames) == 1
    await node.stop()


async def test_transport_error_aborts_batch(demo):
    peer, node = demo
    peer.refuse = True
    first = node.write32(0x0, 1)
    second = node.read32(0x0)
    with pytest.raises(ConnectionRefusedError):
        await first
    with pytest.raises(ConnectionRefusedError):
        await second
    peer.refuse = False
    await node.write32(0x0, 0x04030201)
    assert await node.read32(0x0) == 0x04030201


async def test_out_of_address_space(demo):
    peer, node = demo
    bad = node.mem_read(0xfffe, 4)
    good = node.write32(0xfffc, 0x01020304)
    with pytest.raises(ValueError, match="16-bit"):
        await bad
    await good
    assert peer.frames == [b"\x00\xfc\xff\x04\x03\x02\x01"]


async def test_empty_ops(demo):
    peer, node = demo
    assert await node.mem_read(0x0, 0) == b""
    await node.mem_write(0x0, b"")
    assert peer.frames == []


async def test_no_wait_ops(demo):
    peer, node = demo
    node.post_no_wait(memory.WriteBlob(0x40, b"\x42" * 4))
    assert await node.read32(0x40) == 0x42424242


async def test_unexpected_answer_dropped(demo):
    peer, node = demo
    peer.emit(b"\x00")
    await asyncio.sleep(0)
    await node.write32(0x0, 7)
    assert await node.read32(0x0) == 7


@pytest.mark.parametrize("opts", [
    "address_width=16,data_bus_width=32",
    "address_width=16,burst_length_l2=4",
    "data_bus_width=32,burst_length_l2=4",
    "address_width=16,data_bus_width=12,burst_length_l2=4",
    "address_width=16,data_bus_width=32,burst_length_l2=4,max_write=0",
    "address_width=16,data_bus_width=32,burst_length_l2=4,window=0",
    "address_width=16,data_bus_width=32,burst_length_l2=4,timeout=0",
])
async def test_bad_options(opts):
    peer = FakeBridge()
    with pytest.raises(ValueError):
        await summon(peer, f"nsl_apb({opts})")

