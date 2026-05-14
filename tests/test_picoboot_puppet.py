"""Tests for `acrobe.component.raspberry.picoboot.PicobootPuppet`.

The mock transport models RP2040 SRAM as a bytearray and
interprets PICOBOOT EXEC by parsing the puppet's own trampoline
layout: read the data_addr literal at thunk+20, read fn_pc and
args from the data area, dispatch to a Python function registered
at fn_pc, and write the function's return into the result slot.
A bug in the puppet's trampoline ABI would surface here as the
mock failing to find the data area or function.
"""

import asyncio
import struct

import pytest

from acrobe.target.puppet import Puppet
from acrobe.target.region import Ram
from acrobe.component.raspberry.picoboot import PicobootPuppet


RAM_BASE = 0x20000000
RAM_SIZE = 0x10000


class MockPicobootTransport:
    def __init__(self, ram_base=RAM_BASE, ram_size=RAM_SIZE):
        self.ram_base = ram_base
        self.ram = bytearray(ram_size)
        self.read_log: list[tuple[int, int]] = []
        self.write_log: list[tuple[int, bytes]] = []
        self.exec_log: list[int] = []
        # fn_pc (Thumb bit stripped) → sync callable(*args) -> int.
        self.functions: dict[int, callable] = {}
        # Awaitable delay before each exec, for timeout tests.
        self.exec_delay: float = 0.0

    def offset(self, addr):
        off = addr - self.ram_base
        assert 0 <= off < len(self.ram), f"addr 0x{addr:08x} out of mock RAM"
        return off

    async def read(self, addr, size):
        self.read_log.append((addr, size))
        off = self.offset(addr)
        return bytes(self.ram[off:off + size])

    async def write(self, addr, data):
        data = bytes(data)
        self.write_log.append((addr, data))
        off = self.offset(addr)
        self.ram[off:off + len(data)] = data

    async def exec(self, pc):
        self.exec_log.append(pc)
        if self.exec_delay > 0:
            await asyncio.sleep(self.exec_delay)
        thunk_off = self.offset(pc & ~1)
        # Trampoline ABI: data_addr literal lives at thunk + 20.
        data_addr = struct.unpack(
            "<I", self.ram[thunk_off + 20:thunk_off + 24])[0]
        data_off = self.offset(data_addr)
        fn_pc, a0, a1, a2, a3 = struct.unpack(
            "<5I", self.ram[data_off:data_off + 20])
        fn = self.functions[fn_pc & ~1]
        result = fn(a0, a1, a2, a3) & 0xFFFFFFFF
        self.ram[data_off + 20:data_off + 24] = struct.pack("<I", result)


def make_puppet():
    ram = Ram("sram", RAM_BASE, RAM_SIZE)
    transport = MockPicobootTransport()
    puppet = PicobootPuppet("picoboot", ram, transport)
    return puppet, transport


class TestPicobootPuppet:
    @pytest.mark.asyncio
    async def test_satisfies_puppet_protocol(self):
        puppet, _ = make_puppet()
        assert isinstance(puppet, Puppet)

    @pytest.mark.asyncio
    async def test_call_returns_function_result(self):
        puppet, transport = make_puppet()
        # A fake "add" function at a known target address.
        ADD_PC = 0x20008000
        transport.functions[ADD_PC] = (
            lambda a, b, c, d: (a + b + c + d) & 0xFFFFFFFF)
        result = await puppet.call(ADD_PC | 1, 1, 2, 3, 4)
        assert result == 10

    @pytest.mark.asyncio
    async def test_install_writes_thunk_once(self):
        puppet, transport = make_puppet()
        PC = 0x20008000
        transport.functions[PC] = lambda *args: 0
        await puppet.call(PC, 0)
        await puppet.call(PC, 0)
        # Trampoline write address appears in write_log exactly once.
        trampoline_writes = [
            w for w in transport.write_log
            if w[0] == puppet.trampoline.address
        ]
        assert len(trampoline_writes) == 1
        # And the bytes match: thunk code + data_addr literal.
        expected = (PicobootPuppet.THUNK_CODE
                    + struct.pack("<I", puppet.data.address))
        assert trampoline_writes[0][1] == expected

    @pytest.mark.asyncio
    async def test_thunk_bit_set_on_exec(self):
        puppet, transport = make_puppet()
        PC = 0x20008000
        transport.functions[PC] = lambda *_: 0
        await puppet.call(PC, 0)
        # The PC passed to EXEC must have the Thumb bit set.
        assert len(transport.exec_log) == 1
        assert transport.exec_log[0] & 1 == 1
        assert transport.exec_log[0] & ~1 == puppet.trampoline.address

    @pytest.mark.asyncio
    async def test_thumb_bit_set_on_fn_pc(self):
        puppet, transport = make_puppet()
        PC = 0x20008000
        await puppet.prepare(PC, 0, 0, 0, 0)
        fn_pc = struct.unpack(
            "<I", transport.ram[
                transport.offset(puppet.data.address):
                transport.offset(puppet.data.address) + 4])[0]
        assert fn_pc == PC | 1

    @pytest.mark.asyncio
    async def test_fewer_than_four_args_padded_with_zero(self):
        puppet, transport = make_puppet()
        PC = 0x20008000
        captured = []
        transport.functions[PC] = (
            lambda a, b, c, d: captured.append((a, b, c, d)) or 0)
        await puppet.call(PC, 0xdeadbeef)
        assert captured == [(0xdeadbeef, 0, 0, 0)]

    @pytest.mark.asyncio
    async def test_too_many_args_rejected(self):
        puppet, transport = make_puppet()
        with pytest.raises(ValueError):
            await puppet.prepare(0x20008000, 1, 2, 3, 4, 5)

    @pytest.mark.asyncio
    async def test_run_twice_without_wait_rejected(self):
        puppet, transport = make_puppet()
        PC = 0x20008000
        transport.functions[PC] = lambda *_: 0
        # Hold exec pending so the second run() sees a live task.
        transport.exec_delay = 0.05
        await puppet.prepare(PC)
        await puppet.run()
        try:
            with pytest.raises(RuntimeError):
                await puppet.run()
        finally:
            await puppet.wait()

    @pytest.mark.asyncio
    async def test_wait_without_run_rejected(self):
        puppet, _ = make_puppet()
        with pytest.raises(RuntimeError):
            await puppet.wait()

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        puppet, transport = make_puppet()
        PC = 0x20008000
        transport.functions[PC] = lambda *_: 0
        transport.exec_delay = 0.2
        await puppet.prepare(PC)
        await puppet.run()
        with pytest.raises(TimeoutError):
            await puppet.wait(timeout=0.05)

    @pytest.mark.asyncio
    async def test_stub_install_and_call(self):
        """`PuppetStub` works on PicobootPuppet — installs once,
        the installed zone lives at a known RAM address, and
        calling routes through prepare/run/wait."""
        puppet, transport = make_puppet()
        stub_bytes = b"\x70\x47" * 4  # 8 bytes of "bx lr" filler
        stub = puppet.stub(stub_bytes, name="probe")
        # Register the function at the stub's allocated address.
        transport.functions[stub.zone.address] = lambda *_: 0x1234abcd
        result = await stub.call()
        assert result == 0x1234abcd
        # Stub's code was uploaded to its zone.
        zone_off = transport.offset(stub.zone.address)
        assert (transport.ram[zone_off:zone_off + len(stub_bytes)]
                == stub_bytes)
        # Second call doesn't re-upload.
        before_writes = len(transport.write_log)
        await stub.call()
        # Two writes per call: data area + (no install). No new
        # stub-bytes write.
        new_writes = transport.write_log[before_writes:]
        assert all(addr != stub.zone.address for addr, _ in new_writes)

    @pytest.mark.asyncio
    async def test_mem_read_write_passthrough(self):
        puppet, transport = make_puppet()
        await puppet.mem_write(0x20009000, b"\x01\x02\x03\x04")
        data = await puppet.mem_read(0x20009000, 4)
        assert data == b"\x01\x02\x03\x04"

    @pytest.mark.asyncio
    async def test_trampoline_and_data_non_overlapping(self):
        puppet, _ = make_puppet()
        t_end = puppet.trampoline.address + puppet.trampoline.size
        d_end = puppet.data.address + puppet.data.size
        assert (puppet.trampoline.address >= d_end
                or puppet.data.address >= t_end)
