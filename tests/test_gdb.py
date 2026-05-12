"""Tests for the GDB Remote Serial Protocol responder."""

import binascii
import pytest

from acrobe.memory_map import MemoryMap
from acrobe.target.debuggable import (
    Core, CoreState, Debuggable, HaltCause, Register, RegisterType,
)
from acrobe.target.gdb import message
from acrobe.target.gdb.message import Packet
from acrobe.target.gdb.protocol import Responder
from acrobe.target.loadable import Loadable
from acrobe.target.region import Flash, Ram


# -- Mocks ----------------------------------------------------------

class FakeCore(Core):
    gdb_feature_name = "org.gnu.gdb.arm.m-profile"
    gdb_byteorder = "little"

    def __init__(self, name="core", *, registers=None):
        super().__init__(name)
        self.registers = registers or [
            Register(0, "r0", 32, RegisterType.GPR, "general"),
            Register(1, "r1", 32, RegisterType.GPR, "general"),
            Register(15, "pc", 32, RegisterType.PC, "general"),
        ]
        self.values = {r: 0 for r in self.registers}
        self.state_value = CoreState.HALT
        self.halt_cause_value = HaltCause.DEBUGGER
        self.breakpoints = []
        self.history = []

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
        return self.state_value

    async def halt_cause(self):
        return self.halt_cause_value

    async def halt(self):
        self.history.append("halt")
        self.state_value = CoreState.HALT

    async def resume(self, *, allow_interrupts=True):
        self.history.append("resume")
        self.state_value = CoreState.HALT  # Pretend we land back at a bp

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

    async def breakpoint_add(self, addr, kind):
        bp = (len(self.breakpoints), addr, kind)
        self.breakpoints.append(bp)
        return bp

    async def breakpoint_remove(self, bp):
        self.breakpoints = [b for b in self.breakpoints if b[1] != bp[1]]

    async def breakpoint_list(self):
        return list(self.breakpoints)


class FakeDebuggable(Debuggable):
    def __init__(self, *, cores=None, memory_map=None):
        super().__init__("debug")
        for c in cores or [FakeCore()]:
            self.child_add(c)
        self.memory = bytearray(0x10000)
        self.memory_map = memory_map or []
        self.attached = False
        self.monitor_log = []

    async def attach(self):
        self.attached = True

    async def detach(self):
        self.attached = False

    async def mem_read(self, addr, size):
        return bytes(self.memory[addr:addr + size])

    async def mem_write(self, addr, data):
        self.memory[addr:addr + len(data)] = data

    async def monitor(self, cmd, args):
        self.monitor_log.append((cmd, list(args)))
        if cmd == "reset":
            return "Target reset.\n"
        raise NotImplementedError


class FakeLoadable(Loadable):
    """Loadable wrapper used by vFlashErase/Write tests."""

    def __init__(self):
        super().__init__("flash")
        self.flash_region = FakeFlash("main", 0x08000000, 0x10000, 256)
        self.child_add(self.flash_region)
        self.write_calls = []

    async def write(self, source, **kw):
        m = await self._coerce(source)
        self.write_calls.append((m, kw))

    @staticmethod
    async def _coerce(source):
        if isinstance(source, MemoryMap):
            return source
        raise TypeError(source)


class FakeFlash(Flash):
    def __init__(self, name, address, size, page):
        super().__init__(name, address, size, page, [page])

    async def read(self, offset, size):
        return b"\xff" * size

    async def write(self, offset, data):
        pass

    async def erase(self, offset, size):
        pass


# -- Packet framing --------------------------------------------------

class TestPacket:
    @pytest.mark.parametrize("raw,framed", [
        (b"OK", b"$OK#9a"),
        (b"E01", b"$E01#a6"),
        (b"", b"$#00"),
    ])
    def test_frame(self, raw, framed):
        assert Packet.frame(raw) == framed

    def test_parse_valid(self):
        assert Packet.parse(b"$OK#9a") == b"OK"

    def test_parse_invalid_checksum(self):
        assert Packet.parse(b"$OK#00") is None

    def test_parse_missing_hash(self):
        assert Packet.parse(b"$abc") is None

    def test_parse_short(self):
        assert Packet.parse(b"$#") is None

    def test_escape_unescape_roundtrip(self):
        # The four reserved bytes get escaped.
        raw = bytes([0x23, 0x24, 0x2A, 0x7D, 0x41])
        escaped = Packet.escape(raw)
        assert Packet.unescape(escaped) == raw

    def test_unescape_rle(self):
        # 'X' '*' 32 means X repeated 4 more times → 5 X's total.
        # encoded count 32 (' ') = (32 - 29) + 1 = 4.
        assert Packet.unescape(b"X*\x20") == b"XXXXX"

    def test_unframe_helper(self):
        framed = Packet.frame(b"hello")
        assert message.unframe(framed) == b"hello"


# -- Responder dispatch ----------------------------------------------

@pytest.fixture
def responder():
    debug = FakeDebuggable()
    return Responder(debug)


class TestResponderQuery:
    @pytest.mark.asyncio
    async def test_question_returns_stop_reason(self, responder):
        resp = await responder.handle_packet(b"?")
        assert resp == b"T05hwbreak:;"

    @pytest.mark.asyncio
    async def test_q_supported(self, responder):
        resp = await responder.handle_packet(b"qSupported:multiprocess+")
        assert b"PacketSize=" in resp
        assert b"QStartNoAckMode+" in resp

    @pytest.mark.asyncio
    async def test_qC_returns_current_thread(self, responder):
        resp = await responder.handle_packet(b"qC")
        assert resp == b"QC1"

    @pytest.mark.asyncio
    async def test_qfThreadInfo(self, responder):
        resp = await responder.handle_packet(b"qfThreadInfo")
        assert resp == b"m1"

    @pytest.mark.asyncio
    async def test_qsThreadInfo_terminates(self, responder):
        resp = await responder.handle_packet(b"qsThreadInfo")
        assert resp == b"l"

    @pytest.mark.asyncio
    async def test_qAttached(self, responder):
        resp = await responder.handle_packet(b"qAttached")
        assert resp == b"1"

    @pytest.mark.asyncio
    async def test_qXfer_features_target_xml(self, responder):
        resp = await responder.handle_packet(
            b"qXfer:features:read:target.xml:0,1000")
        assert resp.startswith(b"l<")
        assert b"<target>" in resp
        assert b"r0" in resp and b"pc" in resp

    @pytest.mark.asyncio
    async def test_qstart_no_ack_mode_sets_flag(self, responder):
        resp = await responder.handle_packet(b"QStartNoAckMode")
        assert resp == b"OK"
        assert responder.no_ack_mode_requested


class TestResponderRegs:
    @pytest.mark.asyncio
    async def test_p_reads_one_register(self, responder):
        responder.current_core.values[
            responder.current_core.lookup_register("r0")] = 0xDEADBEEF
        resp = await responder.handle_packet(b"p0")
        # little-endian bytes: ef be ad de → "efbeadde"
        assert resp == b"efbeadde"

    @pytest.mark.asyncio
    async def test_g_reads_all_registers(self, responder):
        for i, r in enumerate(responder.current_core.registers):
            responder.current_core.values[r] = 0x10 + i
        resp = await responder.handle_packet(b"g")
        # Three 32-bit regs little-endian → 24 hex chars.
        assert len(resp) == 24
        # First reg = 0x10 → "10000000".
        assert resp[:8] == b"10000000"

    @pytest.mark.asyncio
    async def test_P_writes_one_register(self, responder):
        resp = await responder.handle_packet(b"P0=78563412")
        assert resp == b"OK"
        r0 = responder.current_core.lookup_register("r0")
        assert responder.current_core.values[r0] == 0x12345678

    @pytest.mark.asyncio
    async def test_G_writes_all_registers(self, responder):
        # Three regs, 4 bytes each, little-endian: 0x01 0x02 0x03.
        resp = await responder.handle_packet(b"G010000000200000003000000")
        assert resp == b"OK"
        regs = responder.current_core.registers
        assert responder.current_core.values[regs[0]] == 1
        assert responder.current_core.values[regs[1]] == 2
        assert responder.current_core.values[regs[2]] == 3

    @pytest.mark.asyncio
    async def test_G_xs_skip_register(self, responder):
        # 'xx...' in a slot means "leave this register alone".
        regs = responder.current_core.registers
        responder.current_core.values[regs[1]] = 0xAA
        resp = await responder.handle_packet(b"G01000000xxxxxxxx03000000")
        assert resp == b"OK"
        assert responder.current_core.values[regs[0]] == 1
        assert responder.current_core.values[regs[1]] == 0xAA  # unchanged
        assert responder.current_core.values[regs[2]] == 3


class TestResponderMemory:
    @pytest.mark.asyncio
    async def test_m_reads_memory(self, responder):
        responder.debuggable.memory[0x100:0x104] = b"\xaa\xbb\xcc\xdd"
        resp = await responder.handle_packet(b"m100,4")
        assert resp == b"aabbccdd"

    @pytest.mark.asyncio
    async def test_M_writes_memory_hex(self, responder):
        resp = await responder.handle_packet(b"M100,4:01020304")
        assert resp == b"OK"
        assert bytes(responder.debuggable.memory[0x100:0x104]) == b"\x01\x02\x03\x04"

    @pytest.mark.asyncio
    async def test_X_writes_memory_binary(self, responder):
        # X uses raw bytes after the colon (already unescaped by Packet).
        resp = await responder.handle_packet(b"X100,3:\xde\xad\xbe")
        assert resp == b"OK"
        assert bytes(responder.debuggable.memory[0x100:0x103]) == b"\xde\xad\xbe"


class TestResponderRunControl:
    @pytest.mark.asyncio
    async def test_c_calls_resume(self, responder):
        await responder.handle_packet(b"c")
        assert "resume" in responder.current_core.history

    @pytest.mark.asyncio
    async def test_s_calls_step(self, responder):
        await responder.handle_packet(b"s")
        assert "step" in responder.current_core.history

    @pytest.mark.asyncio
    async def test_R_resets(self, responder):
        await responder.handle_packet(b"R0")
        assert any("reset" in h for h in responder.current_core.history)

    @pytest.mark.asyncio
    async def test_interrupt_halts_and_returns_stop_reason(self, responder):
        responder.current_core.state_value = CoreState.RUN
        responder.current_core.history.clear()
        resp = await responder.handle_interrupt()
        assert "halt" in responder.current_core.history
        # Halt flipped the state; halt_cause is DEBUGGER → T05hwbreak.
        assert resp == b"T05hwbreak:;"


class TestResponderBreakpoints:
    @pytest.mark.asyncio
    async def test_Z1_adds_breakpoint(self, responder):
        resp = await responder.handle_packet(b"Z1,8000100,2")
        assert resp == b"OK"
        assert (0, 0x8000100, 2) in responder.current_core.breakpoints

    @pytest.mark.asyncio
    async def test_z1_removes_breakpoint(self, responder):
        await responder.handle_packet(b"Z1,8000100,2")
        resp = await responder.handle_packet(b"z1,8000100,2")
        assert resp == b"OK"
        assert responder.current_core.breakpoints == []


class TestResponderThreads:
    @pytest.mark.asyncio
    async def test_H_switches_core(self):
        c1 = FakeCore("c1")
        c2 = FakeCore("c2")
        debug = FakeDebuggable(cores=[c1, c2])
        responder = Responder(debug)
        assert responder.current_core is c1
        resp = await responder.handle_packet(b"Hg2")
        assert resp == b"OK"
        assert responder.current_core is c2

    @pytest.mark.asyncio
    async def test_H_zero_picks_first(self):
        c1 = FakeCore("c1")
        c2 = FakeCore("c2")
        debug = FakeDebuggable(cores=[c1, c2])
        responder = Responder(debug)
        await responder.handle_packet(b"Hg2")
        resp = await responder.handle_packet(b"Hg0")
        assert resp == b"OK"
        assert responder.current_core is c1


class TestResponderMonitor:
    @pytest.mark.asyncio
    async def test_qRcmd_invokes_monitor(self, responder):
        cmd_hex = b"reset".hex().encode("ascii")
        resp = await responder.handle_packet(b"qRcmd," + cmd_hex)
        # Reply is hex-encoded.
        text = binascii.a2b_hex(resp).decode("utf-8")
        assert "reset" in text.lower()
        assert ("reset", []) in responder.debuggable.monitor_log

    @pytest.mark.asyncio
    async def test_qRcmd_unknown_returns_message(self, responder):
        cmd_hex = b"asdf".hex().encode("ascii")
        resp = await responder.handle_packet(b"qRcmd," + cmd_hex)
        text = binascii.a2b_hex(resp).decode("utf-8")
        assert "Unknown" in text


class TestResponderMemoryMap:
    @pytest.mark.asyncio
    async def test_memory_map_xml_includes_flash_and_ram(self):
        debug = FakeDebuggable(memory_map=[
            Ram("sram", 0x20000000, 0x10000),
        ])
        loadable = FakeLoadable()
        responder = Responder(debug, loadable)
        resp = await responder.handle_packet(
            b"qXfer:memory-map:read::0,2000")
        assert resp.startswith(b"l<")
        assert b'type="ram"' in resp
        assert b'type="flash"' in resp
        assert b'<property name="blocksize">0x100</property>' in resp


class TestResponderFlash:
    @pytest.mark.asyncio
    async def test_vFlashErase_then_Write_then_Done_routes_to_loadable(self):
        debug = FakeDebuggable()
        loadable = FakeLoadable()
        responder = Responder(debug, loadable)
        await responder.handle_packet(b"vFlashErase:8000000,1000")
        await responder.handle_packet(b"vFlashWrite:8000000:\x00\x01\x02\x03")
        resp = await responder.handle_packet(b"vFlashDone")
        assert resp == b"OK"
        assert len(loadable.write_calls) == 1
        mm, kw = loadable.write_calls[0]
        assert kw["do_erase"] is True
        assert list(mm) == [(0x8000000, b"\x00\x01\x02\x03")]

    @pytest.mark.asyncio
    async def test_vFlashErase_no_loadable_errors(self):
        debug = FakeDebuggable()
        responder = Responder(debug)
        resp = await responder.handle_packet(b"vFlashErase:8000000,1000")
        assert resp == b"E01"


class TestServer:
    """End-to-end through the asyncio TCP server."""

    @pytest.mark.asyncio
    async def test_server_handles_one_session(self):
        import asyncio
        from acrobe.target.gdb import GdbServer

        debug = FakeDebuggable()
        server = GdbServer(debug, host="127.0.0.1", port=0)
        # Start the server on a random port (port=0).
        await server.start()
        sockets = server._GdbServer__server.sockets
        port = sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(message.frame(b"qSupported"))
            await writer.drain()
            # We expect an ACK (+) followed by the framed reply.
            ack = await reader.readexactly(1)
            assert ack == b"+"
            framed = await reader.readuntil(b"#")
            cs = await reader.readexactly(2)
            payload = Packet.parse(framed + cs)
            assert b"PacketSize=" in payload
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()


class TestResponderVCont:
    @pytest.mark.asyncio
    async def test_vContQ_lists_supported_actions(self, responder):
        resp = await responder.handle_packet(b"vCont?")
        assert resp == b"vCont;c;s"

    @pytest.mark.asyncio
    async def test_vCont_c_resumes(self, responder):
        resp = await responder.handle_packet(b"vCont;c")
        assert b"T05" in resp or b"S05" in resp
        assert "resume" in responder.current_core.history
