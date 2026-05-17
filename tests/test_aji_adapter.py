"""End-to-end test for the AJI adapter Node tree.

Stands up a hand-rolled libaji-speaking fake server, then drives an
:class:`AjiHost` through acrobe's component tree and verifies that a
Tap's IR/DR shifts arrive at the server as ACCESS_IR / ACCESS_DR
commands with the expected fields.
"""

import asyncio
import struct

import pytest

from acrobe.adapter.aji.wire import (
    Command,
    MUX_COMMAND,
    MUX_FIFO_MIN,
    MessageBuilder,
    MessageReader,
    build_greeting,
    decode_mux_header,
    encode_mux_header,
)
from acrobe.adapter.aji.client import DR_FLAG_CAPTURE, IR_FLAG_CAPTURE

from acrobe.adapter.aji import AjiBroker, AjiEnumerator, AjiHost
from acrobe.adapter.model import HwRoot
from acrobe.protocol.jtag import Dr, Instruction, Tap


# --- Fake jtagd-flavoured server -----------------------------------------


class _FakeJtagd:
    def __init__(self, *, hardwares, devices) -> None:
        self.hardwares = hardwares
        self.devices = devices
        self.commands: list[tuple[int, MessageReader]] = []
        self.fifo_in: list[bytes] = []

    async def serve(self, reader, writer) -> None:
        await self._send(writer, MUX_COMMAND, build_greeting(13, authtype=0))
        try:
            while True:
                hdr = await reader.readexactly(2)
                mux, length = decode_mux_header(hdr)
                payload = await reader.readexactly(length)
                if mux != MUX_COMMAND:
                    self.fifo_in.append(payload)
                    continue
                rdr = MessageReader(payload)
                resps = MessageBuilder()
                while not rdr.at_end:
                    cmd_byte = rdr.next_block()
                    self.commands.append((cmd_byte, payload))
                    fifo_out = self._handle(cmd_byte, rdr, resps)
                    if fifo_out:
                        await self._send(writer, MUX_FIFO_MIN, fifo_out)
                await self._send(writer, MUX_COMMAND, resps.build())
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _send(self, writer, mux: int, payload: bytes) -> None:
        writer.write(encode_mux_header(mux, len(payload)) + payload)
        await writer.drain()

    def _handle(self, cmd, rdr, r):
        if cmd == int(Command.USE_PROTOCOL_VERSION):
            rdr.read_int()
            r.start_response(0).add_int(1)
            return None
        if cmd == int(Command.GET_VERSION_INFO):
            r.start_response(0).add_string("fake jtagd").add_int(0).add_string("/")
            return None
        if cmd == int(Command.GET_HARDWARE):
            rdr.read_int()
            blob = self._enc_hw()
            r.start_response(0).add_int(len(self.hardwares)).add_int(len(blob))
            return blob if blob else None
        if cmd == int(Command.LOCK_CHAIN):
            rdr.read_int(); rdr.read_int()
            r.start_response(0)
            return None
        if cmd == int(Command.UNLOCK_CHAIN):
            rdr.read_int()
            r.start_response(0)
            return None
        if cmd == int(Command.SCAN_CHAIN):
            rdr.read_int(); rdr.read_int()
            r.start_response(0)
            return None
        if cmd == int(Command.READ_CHAIN):
            rdr.read_int(); rdr.read_int(); rdr.read_int()
            blob = self._enc_dev()
            (r.start_response(0)
                .add_int(1).add_int(len(self.devices)).add_int(len(blob)))
            return blob if blob else None
        if cmd == int(Command.OPEN_DEVICE):
            rdr.read_int(); rdr.read_int()
            n = rdr.read_int()
            for _ in range(n):
                # v13: type(int) + length(int) + value(long)
                rdr.read_int(); rdr.read_int(); rdr.read_long()
            rdr.read_string()    # application_name
            r.start_response(0).add_int(7000)
            return None
        if cmd == int(Command.CLOSE_DEVICE):
            rdr.read_int()
            r.start_response(0)
            return None
        if cmd == int(Command.LOCK_DEVICE):
            rdr.read_int(); rdr.read_int()
            r.start_response(0)
            return None
        if cmd == int(Command.UNLOCK_DEVICE):
            rdr.read_int()
            r.start_response(0)
            return None
        if cmd == int(Command.ACCESS_IR):
            rdr.read_int()              # open_id
            instr = rdr.read_int()
            flags = rdr.read_int()
            resp = r.start_response(0)
            if flags & IR_FLAG_CAPTURE:
                resp.add_int(instr ^ 0x42)
            return None
        if cmd == int(Command.ACCESS_DR):
            rdr.read_int()           # open_id
            length_dr = rdr.read_int()
            rdr.read_int()           # flags
            rdr.read_int(); rdr.read_int()  # write_offset, write_length
            rdr.read_int()           # read_offset
            read_length = rdr.read_int()
            if rdr.remaining >= 4:
                rdr.read_int()       # batch
            r.start_response(0)
            if read_length > 0:
                # Use the length to encode an observable response so
                # tests can assert what acrobe got.
                return bytes([0x55] * ((read_length + 7) // 8))
            return None
        if cmd == int(Command.RUN_TEST_IDLE):
            rdr.read_int(); rdr.read_int()
            if rdr.remaining >= 4:
                rdr.read_int()
            r.start_response(0)
            return None
        if cmd == int(Command.TEST_LOGIC_RESET):
            rdr.read_int()
            r.start_response(0)
            return None
        r.start_response(45)
        return None

    def _enc_hw(self):
        out = bytearray()
        for hw in self.hardwares:
            out.extend(struct.pack(">I", hw["chain_id"]))
            out.append(len(hw["hw_name"])); out.extend(hw["hw_name"].encode())
            out.append(len(hw["port"])); out.extend(hw["port"].encode())
            out.extend(struct.pack(">I", hw["chain_type"]))
            out.append(0)
            out.extend(struct.pack(">I", hw["features"]))
        return bytes(out)

    def _enc_dev(self):
        out = bytearray()
        for d in self.devices:
            out.extend(struct.pack(">I", d["idcode"]))
            out.extend(struct.pack(">I", d["irlen"]))
            out.extend(struct.pack(">I", d["features"]))
            out.extend(b"\x00" * 8)
            name = d["name"].encode()
            out.append(len(name)); out.extend(name)
        return bytes(out)


# --- fixture --------------------------------------------------------------


@pytest.fixture
async def fake_server_and_host():
    fake = _FakeJtagd(
        hardwares=[
            dict(chain_id=131073, hw_name="Fake-USB-Blaster",
                 port="USB-1234", chain_type=0, features=0x802),
        ],
        devices=[
            # Synthetic IDCODE — picked to NOT resolve to any
            # registered Tap subclass (ARM JTAG-DP, etc.), so that
            # discovery falls back to the generic Tap.
            dict(idcode=0xdeadbeef, irlen=4, features=0x4, name="ARM-DAP"),
        ],
    )
    asyncio_server = await asyncio.start_server(
        fake.serve, host="127.0.0.1", port=0)
    port = asyncio_server.sockets[0].getsockname()[1]
    host = AjiHost(name=f"127.0.0.1:{port}", host="127.0.0.1", port=port)
    await host.start_tree()
    try:
        yield host, fake
    finally:
        await host.stop_tree()
        asyncio_server.close()
        await asyncio_server.wait_closed()


# --- tests ---------------------------------------------------------------


class TestEnumerator:
    @pytest.mark.asyncio
    async def test_match_only_aji(self):
        from acrobe.db import NoMatch
        with pytest.raises(NoMatch):
            await AjiEnumerator().spawn("usb")


class TestPathResolution:
    @pytest.mark.asyncio
    async def test_broker_parses_host_port(self):
        host = await AjiBroker().child_spawn("127.0.0.1:1310")
        assert host.server_address == ("127.0.0.1", 1310)

    @pytest.mark.asyncio
    async def test_broker_default_port(self):
        from acrobe.adapter.aji.wire import JTAG_PORT
        host = await AjiBroker().child_spawn("localhost")
        assert host.server_address == ("localhost", JTAG_PORT)


class TestEnumeration:
    @pytest.mark.asyncio
    async def test_hardware_attached_after_start(self, fake_server_and_host):
        host, _ = fake_server_and_host
        # hw_name "Fake-USB-Blaster" mangled → "fake-usb-blaster"
        names = [c.name for c in host.children]
        assert names == ["fake-usb-blaster"]

    @pytest.mark.asyncio
    async def test_taps_attached_under_hardware(self, fake_server_and_host):
        host, _ = fake_server_and_host
        hw_node = host.children[0]
        # Synthetic IDCODE doesn't match any Tap.db registration,
        # so discovery falls back to the generic Tap with the
        # positional fallback name.
        assert [c.name for c in hw_node.children] == ["tap0"]

    @pytest.mark.asyncio
    async def test_tap_carries_metadata(self, fake_server_and_host):
        host, _ = fake_server_and_host
        tap = host.children[0].children[0]
        assert tap.idcode == 0xdeadbeef
        assert tap.irlen == 4


class TestJtagOps:
    @pytest.mark.asyncio
    async def test_ir_dr_round_trip(self, fake_server_and_host):
        host, fake = fake_server_and_host

        class TestTap(Tap):
            DATA_REG = Dr(length=8)
            DATA = Instruction(0x05, "DATA_REG")

        # Replace the auto-discovered tap with a typed TestTap so we
        # can call DATA() through the InstructionRegistry. The
        # AjiHardware tracks open_ids per Tap object.
        hw_node = host.children[0]
        old_tap = hw_node.children[0]
        await hw_node.child_remove(old_tap)
        new_tap = TestTap(idcode=old_tap.idcode, irlen=4, name=old_tap.name)
        # Open a fresh device id; lock it; register with the hardware.
        new_open_id = await host.client.open_device(
            hw_node.chain_id, 0, application_name="acrobe-test")
        await host.client.lock_device(new_open_id, 5000)
        hw_node.open_id_of[new_tap] = new_open_id
        hw_node.last_ir[new_tap] = None
        hw_node.locked_devices.add(new_open_id)
        hw_node.child_add(new_tap)

        result = await new_tap.DATA(0xab)
        # The fake returns 0x55 per byte for any read.
        assert int(result) == 0x55
        cmds = [c[0] for c in fake.commands]
        assert int(Command.ACCESS_IR) in cmds
        assert int(Command.ACCESS_DR) in cmds

    @pytest.mark.asyncio
    async def test_run_emits_run_test_idle(self, fake_server_and_host):
        host, fake = fake_server_and_host
        tap = host.children[0].children[0]
        await tap.run(7)
        assert any(c[0] == int(Command.RUN_TEST_IDLE) for c in fake.commands)

    @pytest.mark.asyncio
    async def test_ir_status(self, fake_server_and_host):
        host, fake = fake_server_and_host
        tap = host.children[0].children[0]
        await tap.ir_status()
        ir_cmds = [c for c in fake.commands if c[0] == int(Command.ACCESS_IR)]
        assert ir_cmds, "expected an ACCESS_IR for ir_status()"
