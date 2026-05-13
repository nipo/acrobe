"""End-to-end GDB Remote Serial Protocol tests.

A small in-process GDB RSP client drives a real `GdbServer` over a
TCP loopback connection against a fake Debuggable + Loadable.
Catches packet-flow regressions (acks, framing, interrupt routing,
flash sequencing) that the per-handler unit tests in `test_gdb.py`
can't see in isolation.
"""

from __future__ import annotations

import asyncio
import binascii
import pytest

from acrobe.target.debuggable import (
    Core, CoreState, Debuggable, HaltCause, Register, RegisterType,
)
from acrobe.target.gdb import GdbServer, message
from acrobe.target.gdb.message import Packet
from acrobe.target.loadable import Loadable
from acrobe.target.region import Flash


# -- Test fakes --------------------------------------------------------

class FakeCore(Core):
    gdb_feature_name = "org.gnu.gdb.arm.m-profile"
    gdb_byteorder = "little"

    def __init__(self):
        super().__init__("core")
        self.registers = [
            Register(0,  "r0", 32, RegisterType.GPR, "general"),
            Register(15, "pc", 32, RegisterType.PC,  "general"),
        ]
        self.values = {r: 0 for r in self.registers}
        # state_provider returns the current state on demand; allows
        # tests to swap the source between calls.
        self.state_provider = lambda: CoreState.HALT
        self.halt_cause_value = HaltCause.DEBUGGER
        self.history: list[str] = []

    def lookup_register(self, key):
        if isinstance(key, Register):
            return key
        if isinstance(key, int):
            for r in self.registers:
                if r.number == key:
                    return r
            raise KeyError(key)
        for r in self.registers:
            if r.name == key:
                return r
        raise KeyError(key)

    async def state(self):
        return self.state_provider()

    async def halt_cause(self):
        return self.halt_cause_value

    async def halt(self):
        self.history.append("halt")
        self.state_provider = lambda: CoreState.HALT

    async def resume(self, *, allow_interrupts=True):
        self.history.append("resume")

    async def step(self):
        self.history.append("step")

    async def reset(self, *, stop=True):
        self.history.append(f"reset(stop={stop})")

    async def reg_read(self, regs):
        return {self.lookup_register(r): self.values[self.lookup_register(r)]
                for r in regs}

    async def reg_write(self, reg_values):
        for k, v in reg_values.items():
            self.values[self.lookup_register(k)] = v


class FakeDebuggable(Debuggable):
    def __init__(self):
        super().__init__("debug")
        self.child_add(FakeCore())
        self.memory = bytearray(0x10000)
        self.memory_map = []

    async def attach(self): pass
    async def detach(self): pass

    async def mem_read(self, addr, size):
        return bytes(self.memory[addr:addr + size])

    async def mem_write(self, addr, data):
        self.memory[addr:addr + len(data)] = data

    async def monitor(self, cmd, args):
        return f"acknowledged: {cmd}\n"


class FakeFlash(Flash):
    def __init__(self):
        super().__init__("main", 0x08000000, 0x10000, 256, [256])
        self.storage = bytearray(b"\xff" * self.size)

    async def read(self, offset, size):
        return bytes(self.storage[offset:offset + size])

    async def write(self, offset, data):
        self.storage[offset:offset + len(data)] = data

    async def erase(self, offset, size):
        self.storage[offset:offset + size] = b"\xff" * size


class FakeLoadable(Loadable):
    def __init__(self):
        super().__init__("flash")
        self.child_add(FakeFlash())
        self.write_calls = []

    async def write(self, source, **kw):
        from acrobe.memory_map import MemoryMap
        if isinstance(source, MemoryMap):
            self.write_calls.append((source, kw))


# -- Minimal RSP client ------------------------------------------------

class GdbClient:
    """In-process GDB Remote Serial Protocol client for testing.

    Speaks the subset we need: connect, send packet, receive packet,
    interrupt (raw 0x03 byte). Always uses ack mode initially, then
    negotiates QStartNoAckMode."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.ack_mode = True
        self.buffer = bytearray()

    async def send_packet(self, payload: bytes) -> bytes:
        """Send a packet and read the matching response (skipping
        the +/-ack byte if ack mode is on)."""
        self.writer.write(Packet.frame(payload))
        await self.writer.drain()
        if self.ack_mode:
            ack = await self.__read_one()
            assert ack in (b"+", b"-"), f"expected ack, got {ack!r}"
        return await self.__read_packet()

    async def send_interrupt(self) -> bytes:
        """Send raw 0x03 and read the resulting stop-reply packet."""
        self.writer.write(b"\x03")
        await self.writer.drain()
        return await self.__read_packet()

    async def disable_ack(self) -> None:
        reply = await self.send_packet(b"QStartNoAckMode")
        assert reply == b"OK"
        self.ack_mode = False

    async def __read_one(self) -> bytes:
        if not self.buffer:
            chunk = await self.reader.read(4096)
            if not chunk:
                raise ConnectionError("server closed")
            self.buffer.extend(chunk)
        b = bytes(self.buffer[:1])
        del self.buffer[:1]
        return b

    async def __read_packet(self) -> bytes:
        while True:
            # Find a `$` start byte.
            while True:
                b = await self.__read_one()
                if b == b"$":
                    break
                # Stray +/- between packets — ignore.
            data = bytearray()
            while True:
                b = await self.__read_one()
                if b == b"#":
                    break
                data.extend(b)
            cs1 = await self.__read_one()
            cs2 = await self.__read_one()
            payload = bytes(data)
            # Verify checksum, send ack/nack.
            expected = sum(payload) & 0xFF
            got = int(cs1 + cs2, 16)
            if self.ack_mode:
                self.writer.write(b"+" if got == expected else b"-")
                await self.writer.drain()
            return Packet.unescape(payload)


# -- Server fixture ----------------------------------------------------

class ServerHandle:
    def __init__(self, debug, loadable, server):
        self.debug = debug
        self.loadable = loadable
        self.server = server


@pytest.fixture
async def server_handle():
    debug = FakeDebuggable()
    loadable = FakeLoadable()
    server = GdbServer(debug, loadable, host="127.0.0.1", port=0)
    await server.start()
    yield ServerHandle(debug, loadable, server)
    await server.close()


@pytest.fixture
async def gdb_client(server_handle):
    sockets = server_handle.server._GdbServer__server.sockets
    port = sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = GdbClient(reader, writer)
    try:
        yield server_handle, client
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# -- Tests -------------------------------------------------------------

class TestHandshake:
    @pytest.mark.asyncio
    async def test_qSupported(self, gdb_client):
        _, client = gdb_client
        reply = await client.send_packet(b"qSupported")
        assert b"PacketSize=" in reply
        assert b"QStartNoAckMode+" in reply

    @pytest.mark.asyncio
    async def test_no_ack_mode_handshake(self, gdb_client):
        _, client = gdb_client
        await client.disable_ack()
        # Subsequent packets must not require acks.
        reply = await client.send_packet(b"qC")
        assert reply == b"QC1"


class TestRegistersAndMemory:
    @pytest.mark.asyncio
    async def test_read_register_roundtrip(self, gdb_client):
        sh, client = gdb_client
        core = sh.debug.cores[0]
        core.values[core.lookup_register("r0")] = 0xDEADBEEF
        await client.disable_ack()
        reply = await client.send_packet(b"p0")
        assert reply == b"efbeadde"  # little-endian hex

    @pytest.mark.asyncio
    async def test_write_register(self, gdb_client):
        sh, client = gdb_client
        core = sh.debug.cores[0]
        await client.disable_ack()
        reply = await client.send_packet(b"P0=78563412")
        assert reply == b"OK"
        assert core.values[core.lookup_register("r0")] == 0x12345678

    @pytest.mark.asyncio
    async def test_memory_read_write(self, gdb_client):
        sh, client = gdb_client
        await client.disable_ack()
        reply = await client.send_packet(b"M100,4:aabbccdd")
        assert reply == b"OK"
        assert bytes(sh.debug.memory[0x100:0x104]) == b"\xaa\xbb\xcc\xdd"
        reply = await client.send_packet(b"m100,4")
        assert reply == b"aabbccdd"


class TestContinueAndInterrupt:
    @pytest.mark.asyncio
    async def test_continue_with_immediate_halt(self, gdb_client):
        """Core re-halts as soon as it's resumed — the simulator
        equivalent of a single-instruction breakpoint hit."""
        sh, client = gdb_client
        core = sh.debug.cores[0]
        # resume() leaves state=HALT (state_provider unchanged).
        await client.disable_ack()
        reply = await client.send_packet(b"c")
        assert reply.startswith(b"T05")
        assert "resume" in core.history

    @pytest.mark.asyncio
    async def test_continue_then_interrupt(self, gdb_client):
        """Core would run forever; client sends Ctrl-C and gets a
        stop reply. Verifies the transport's interrupt-byte path."""
        sh, client = gdb_client
        core = sh.debug.cores[0]
        # Make resume leave the core in RUN until something halts it.
        core.state_provider = lambda: CoreState.RUN
        await client.disable_ack()

        # Send the continue packet manually (we don't want to await
        # the reply yet — we need to send 0x03 first).
        client.writer.write(Packet.frame(b"c"))
        await client.writer.drain()
        # Give the server a moment to issue resume(); polling is at
        # 50ms intervals so 20ms is enough to be inside the wait
        # loop.
        await asyncio.sleep(0.02)
        # Now interrupt.
        client.writer.write(b"\x03")
        await client.writer.drain()
        # Read the stop-reply packet.
        reply = await client._GdbClient__read_packet()
        assert reply.startswith(b"T05") or reply.startswith(b"S")
        assert "halt" in core.history

    @pytest.mark.asyncio
    async def test_step(self, gdb_client):
        sh, client = gdb_client
        core = sh.debug.cores[0]
        await client.disable_ack()
        reply = await client.send_packet(b"s")
        assert reply.startswith(b"T") or reply.startswith(b"S")
        assert "step" in core.history


class TestBreakpoints:
    @pytest.mark.asyncio
    async def test_set_and_clear_hw_breakpoint(self, gdb_client):
        sh, client = gdb_client
        core = sh.debug.cores[0]

        # FakeCore needs breakpoint methods for the test; override.
        added = []

        async def bp_add(addr, kind):
            added.append((addr, kind))
            return ("slot0", addr, kind)

        async def bp_remove(bp):
            added.remove((bp[1], bp[2]))

        async def bp_list():
            return [("slot0", a, k) for a, k in added]

        core.breakpoint_add = bp_add
        core.breakpoint_remove = bp_remove
        core.breakpoint_list = bp_list

        await client.disable_ack()
        reply = await client.send_packet(b"Z1,8000100,2")
        assert reply == b"OK"
        assert (0x8000100, 2) in added
        reply = await client.send_packet(b"z1,8000100,2")
        assert reply == b"OK"
        assert added == []


class TestWatchpoints:
    @pytest.mark.asyncio
    async def test_set_and_clear_write_watchpoint(self, gdb_client):
        sh, client = gdb_client
        core = sh.debug.cores[0]

        added = []

        async def wp_add(addr, size, kind):
            added.append((kind, addr, size))
            return (kind, addr, size)

        async def wp_remove(wp):
            added.remove(wp)

        async def wp_list():
            return list(added)

        core.watchpoint_add = wp_add
        core.watchpoint_remove = wp_remove
        core.watchpoint_list = wp_list

        await client.disable_ack()
        # Z2 = write watchpoint, addr 0x20000000, size 4.
        reply = await client.send_packet(b"Z2,20000000,4")
        assert reply == b"OK"
        assert (2, 0x20000000, 4) in added
        reply = await client.send_packet(b"z2,20000000,4")
        assert reply == b"OK"
        assert added == []

    @pytest.mark.asyncio
    async def test_read_and_access_watchpoint_kinds(self, gdb_client):
        sh, client = gdb_client
        core = sh.debug.cores[0]

        added = []
        core.watchpoint_add = lambda addr, size, kind: _make_async(
            (kind, addr, size), added)
        core.watchpoint_remove = lambda wp: _make_async_remove(wp, added)
        core.watchpoint_list = lambda: _make_async_list(added)

        await client.disable_ack()
        assert (await client.send_packet(
            b"Z3,20000004,1")) == b"OK"
        assert (await client.send_packet(
            b"Z4,20000008,2")) == b"OK"
        assert (3, 0x20000004, 1) in added
        assert (4, 0x20000008, 2) in added


def _make_async(value, sink):
    """Helper: synchronously appends to sink and returns a coroutine
    yielding value."""
    sink.append(value)
    async def _r(): return value
    return _r()


def _make_async_remove(wp, sink):
    sink.remove(wp)
    async def _r(): return None
    return _r()


def _make_async_list(sink):
    async def _r(): return list(sink)
    return _r()


class TestFlash:
    @pytest.mark.asyncio
    async def test_flash_program_sequence(self, gdb_client):
        """vFlashErase → vFlashWrite → vFlashDone → routes a
        MemoryMap into the Loadable with do_erase=True."""
        sh, client = gdb_client
        await client.disable_ack()
        reply = await client.send_packet(b"vFlashErase:8000000,100")
        assert reply == b"OK"
        # vFlashWrite payload after the colon is raw bytes (no hex);
        # we wrap in framing manually because our client always
        # escapes-then-frames.
        reply = await client.send_packet(
            b"vFlashWrite:8000000:" + b"\x01\x02\x03\x04")
        assert reply == b"OK"
        reply = await client.send_packet(b"vFlashDone")
        assert reply == b"OK"
        assert len(sh.loadable.write_calls) == 1
        mm, kw = sh.loadable.write_calls[0]
        assert list(mm) == [(0x8000000, b"\x01\x02\x03\x04")]
        assert kw["do_erase"] is True


class TestMonitor:
    @pytest.mark.asyncio
    async def test_qRcmd(self, gdb_client):
        sh, client = gdb_client
        await client.disable_ack()
        cmd_hex = b"reset".hex().encode("ascii")
        reply = await client.send_packet(b"qRcmd," + cmd_hex)
        # Reply is hex-encoded text.
        text = binascii.a2b_hex(reply).decode("utf-8")
        assert "acknowledged" in text.lower()


class TestThreadModel:
    @pytest.mark.asyncio
    async def test_thread_listing_single_core(self, gdb_client):
        _, client = gdb_client
        await client.disable_ack()
        reply = await client.send_packet(b"qfThreadInfo")
        assert reply == b"m1"
        reply = await client.send_packet(b"qsThreadInfo")
        assert reply == b"l"
        reply = await client.send_packet(b"qC")
        assert reply == b"QC1"


class TestTargetXml:
    @pytest.mark.asyncio
    async def test_target_xml_lists_registers(self, gdb_client):
        _, client = gdb_client
        await client.disable_ack()
        reply = await client.send_packet(
            b"qXfer:features:read:target.xml:0,1000")
        assert reply.startswith(b"l<")
        assert b'<feature name="org.gnu.gdb.arm.m-profile">' in reply
        assert b'name="r0"' in reply
        assert b'name="pc"' in reply
