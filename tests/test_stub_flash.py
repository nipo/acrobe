"""Tests for StubFlash with mock puppet."""

import pytest
import struct
from crobe_async.target.arm.memory import StubFlash
from crobe_async.puppet import Zone
from crobe_async.allocator import Range


class MockMemAp:
    """Mock MemAp with byte-level memory."""

    def __init__(self, base=0x08000000, size=0x40000):
        self._base = base
        self._memory = bytearray(b"\xff" * size)

    async def mem_read(self, addr, size):
        off = addr - self._base
        return bytes(self._memory[off:off + size])

    async def mem_write(self, addr, data):
        off = addr - self._base
        self._memory[off:off + len(data)] = data


class MockZone:
    """Mock Zone for tracking writes."""

    def __init__(self, address, size):
        self.address = address
        self.size = size
        self.range = Range(address, size)
        self.data = bytearray(size)

    async def write(self, data, offset=0):
        self.data[offset:offset + len(data)] = data

    async def read(self, size, offset=0):
        return bytes(self.data[offset:offset + size])


class MockPuppetStub:
    """Mock PuppetStub that records calls."""

    def __init__(self, puppet, code):
        self.puppet = puppet
        self.code = code
        self.calls = []
        self._pending = None
        self._cleaned = False

    async def call(self, *args, timeout=None):
        self.calls.append(("call", args))
        # Simulate erase/write effect
        return 0

    async def prepare(self, *args):
        self._pending = args

    async def run(self):
        if self._pending:
            self.calls.append(("run", self._pending))
            self._pending = None

    async def wait(self, timeout=None):
        return 0

    def cleanup(self):
        self._cleaned = True


class MockPuppet:
    """Mock Puppet for StubFlash tests."""

    def __init__(self, ram_base=0x20000000, ram_size=0x4000):
        self._next_alloc = ram_base
        self._zones = []
        self._stubs = []

    def allocate(self, size, align=1):
        addr = self._next_alloc
        if align > 1:
            addr = (addr + align - 1) & ~(align - 1)
        z = MockZone(addr, size)
        self._next_alloc = addr + size
        self._zones.append(z)
        return z

    def free(self, zone):
        pass  # no-op for tests

    def stub(self, code):
        s = MockPuppetStub(self, code)
        self._stubs.append(s)
        return s

    async def crc32_many(self, ranges):
        """Return CRCs that DON'T match anything (forces full update)."""
        return {addr: 0xdeadbeef for addr, _ in ranges}


DUMMY_ERASE = b"\x00" * 8
DUMMY_WRITE = b"\x00" * 8


class ConcreteStubFlash(StubFlash):
    RANGE_ERASE = DUMMY_ERASE
    PAGE_WRITE = DUMMY_WRITE


class TestStubFlash:
    def _make(self, page_size=256):
        ap = MockMemAp()
        puppet = MockPuppet()
        f = ConcreteStubFlash(
            "flash", 0x08000000, 0x40000,
            write_page_size=page_size,
            erase_page_sizes=[4096],
            mem_ap=ap,
            puppet=puppet,
        )
        return f, ap, puppet

    def test_is_flash(self):
        f, _, _ = self._make()
        assert isinstance(f, StubFlash)

    @pytest.mark.asyncio
    async def test_read_delegates_to_meemap(self):
        f, ap, _ = self._make()
        ap._memory[0:4] = b"\xaa\xbb\xcc\xdd"

        data = await f.read(0, 4)
        assert data == b"\xaa\xbb\xcc\xdd"

    @pytest.mark.asyncio
    async def test_erase_calls_stub(self):
        f, _, puppet = self._make()
        await f.erase(0, 4096)

        # Should have created a stub and called it
        assert len(puppet._stubs) >= 1
        erase_stub = puppet._stubs[0]
        assert len(erase_stub.calls) == 1
        assert erase_stub.calls[0][0] == "call"
        assert erase_stub._cleaned

    @pytest.mark.asyncio
    async def test_erase_full_sets_blank(self):
        f, _, _ = self._make()
        assert not f.is_blank
        await f.erase(0, f.size)
        assert f.is_blank

    @pytest.mark.asyncio
    async def test_erase_partial_not_blank(self):
        f, _, _ = self._make()
        await f.erase(0, 4096)
        assert not f.is_blank

    @pytest.mark.asyncio
    async def test_write_erases_if_not_blank(self):
        f, _, puppet = self._make()

        data = b"\xaa" * 256
        await f.write(0, data)

        # Should create 2 stubs: one for erase, one for write
        stubs = puppet._stubs
        assert len(stubs) >= 2  # erase stub + write stub

    @pytest.mark.asyncio
    async def test_write_skips_erase_if_blank(self):
        f, _, puppet = self._make()
        f.is_blank = True

        data = b"\xaa" * 256
        await f.write(0, data)

        # Only write stub, no erase
        stubs = puppet._stubs
        assert len(stubs) == 1  # just write stub

    @pytest.mark.asyncio
    async def test_puppet_write_double_buffer(self):
        """Test that puppet_write uses double buffering."""
        f, _, puppet = self._make(page_size=256)

        pages = {
            0x08000000: b"\xaa" * 256,
            0x08000100: b"\xbb" * 256,
            0x08000200: b"\xcc" * 256,
        }
        await f.puppet_write(pages)

        # Should have created a write stub
        write_stubs = [s for s in puppet._stubs if s.code == DUMMY_WRITE]
        assert len(write_stubs) == 1
        ws = write_stubs[0]

        # Should have 3 run operations (one per page)
        run_calls = [c for c in ws.calls if c[0] == "run"]
        assert len(run_calls) == 3
        assert ws._cleaned

    @pytest.mark.asyncio
    async def test_puppet_update_skips_matching_crcs(self):
        """puppet_update should skip pages whose CRCs match."""
        f, _, puppet = self._make(page_size=256)

        import binascii
        page_data = b"\xaa" * 256
        expected_crc = binascii.crc32(page_data) & 0xffffffff

        # Make crc32_many return matching CRC for first page
        async def crc32_many(ranges):
            result = {}
            for addr, _ in ranges:
                if addr == 0x08000000:
                    result[addr] = expected_crc  # match
                else:
                    result[addr] = 0xdeadbeef  # mismatch
            return result

        puppet.crc32_many = crc32_many

        pages = {
            0x08000000: page_data,
            0x08000100: b"\xbb" * 256,
        }
        await f.puppet_update(pages)

        # First page should have been removed (CRC matched)
        assert 0x08000000 not in pages
        # Second page should remain (CRC didn't match) — write happened
        write_stubs = [s for s in puppet._stubs if s.code == DUMMY_WRITE]
        assert len(write_stubs) == 1

    @pytest.mark.asyncio
    async def test_puppet_update_empty(self):
        f, _, puppet = self._make()
        await f.puppet_update({})
        assert len(puppet._stubs) == 0
