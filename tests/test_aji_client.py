"""End-to-end sanity test for the AjiClient low-level driver.

Spins up a tiny server that speaks libaji wire format directly and
checks that AjiClient round-trips greeting, version negotiation,
hardware enumeration, scan_chain, OPEN_DEVICE / lock / IR / DR /
run_test_idle / test_logic_reset.

This test demonstrates the wire/link/client stack talks the protocol
Intel publishes — which is the same one real ``jtagd`` speaks.
"""

import asyncio
import struct

import pytest

from acrobe.adapter.aji.wire import (
    AJI_CURRENT_VERSION,
    Command,
    MUX_COMMAND,
    MUX_FIFO_MIN,
    MessageBuilder,
    MessageReader,
    build_greeting,
    decode_mux_header,
    encode_mux_header,
)
from acrobe.adapter.aji.client import (
    AjiClient,
    DR_FLAG_CAPTURE,
    IR_FLAG_CAPTURE,
)


# --- Tiny libaji server fake ----------------------------------------------


class _FakeServer:
    """Pretends to be jtagd. Records every command and returns
    deterministic responses driven by the test."""

    def __init__(self, *,
                 hardwares=None,
                 devices=None,
                 dr_responses=None,
                 parameters=None) -> None:
        self.hardwares = hardwares or []
        self.devices = devices or []
        # Map from access_dr's read_length (bits) → bytes to return on FIFO.
        self.dr_responses = dr_responses or {}
        # Map (chain_id, name) → int value, both for GET_PARAMETER answers
        # and to record SET_PARAMETER calls.
        self.parameters = parameters or {}
        self.commands = []  # list of (cmd_byte, raw_block_payload)
        self.pending_fifo = []  # bytes received from client on mux 4

    async def serve(self, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter) -> None:
        # Greeting
        greeting_payload = build_greeting(
            server_version=AJI_CURRENT_VERSION, authtype=0)
        await self._write_packet(writer, MUX_COMMAND, greeting_payload)

        try:
            while True:
                hdr = await reader.readexactly(2)
                mux, length = decode_mux_header(hdr)
                payload = await reader.readexactly(length)

                if mux != MUX_COMMAND:
                    # Client-pushed FIFO data (e.g. ACCESS_DR write bits).
                    self.pending_fifo.append(payload)
                    continue

                rdr = MessageReader(payload)
                responses = MessageBuilder()
                while not rdr.at_end:
                    cmd_byte = rdr.next_block()
                    self.commands.append((cmd_byte, payload))
                    fifo_to_send = self._handle(cmd_byte, rdr, responses)
                    if fifo_to_send is not None:
                        await self._write_packet(
                            writer, MUX_FIFO_MIN, fifo_to_send)
                await self._write_packet(
                    writer, MUX_COMMAND, responses.build())
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _write_packet(self, writer: asyncio.StreamWriter,
                            mux: int, payload: bytes) -> None:
        writer.write(encode_mux_header(mux, len(payload)) + payload)
        await writer.drain()

    # ---- per-command handlers (return optional FIFO bytes to push) ----

    def _handle(self, cmd: int, rdr: MessageReader,
                responses: MessageBuilder) -> bytes | None:
        if cmd == int(Command.USE_PROTOCOL_VERSION):
            _ = rdr.read_int()
            responses.start_response(0).add_int(1)  # SERVER_ALLOW_REMOTE
            return None

        if cmd == int(Command.GET_VERSION_INFO):
            (responses.start_response(0)
                .add_string("Version 99.9 Build 0 fake jtagd")
                .add_int(0)                          # pgmparts_version
                .add_string("/fake/path"))
            return None

        if cmd == int(Command.GET_HARDWARE):
            _ = rdr.read_int()  # client version
            blob = self._encode_hardware_records(self.hardwares)
            (responses.start_response(0)
                .add_int(len(self.hardwares))
                .add_int(len(blob)))
            return blob if blob else None

        if cmd == int(Command.LOCK_CHAIN):
            _ = rdr.read_int()
            _ = rdr.read_int()
            responses.start_response(0)
            return None

        if cmd == int(Command.UNLOCK_CHAIN):
            _ = rdr.read_int()
            responses.start_response(0)
            return None

        if cmd == int(Command.SET_PARAMETER):
            chain_id = rdr.read_int()
            name = rdr.read_string()
            value = rdr.read_int()
            self.parameters[(chain_id, name)] = value
            responses.start_response(0)
            return None

        if cmd == int(Command.GET_PARAMETER):
            chain_id = rdr.read_int()
            name = rdr.read_string()
            value = self.parameters.get((chain_id, name), 0)
            responses.start_response(0).add_int(value)
            return None

        if cmd == int(Command.SCAN_CHAIN):
            _ = rdr.read_int()
            _ = rdr.read_int()
            responses.start_response(0)
            return None

        if cmd == int(Command.READ_CHAIN):
            _ = rdr.read_int()  # chain_id
            _ = rdr.read_int()  # scan_tag
            _ = rdr.read_int()  # pack_style
            blob = self._encode_device_records(self.devices)
            (responses.start_response(0)
                .add_int(1)                          # scan_tag
                .add_int(len(self.devices))
                .add_int(len(blob)))
            return blob if blob else None

        if cmd == int(Command.OPEN_DEVICE):
            _ = rdr.read_int()  # chain_id
            _ = rdr.read_int()  # tap_position
            n_claims = rdr.read_int()
            for _ in range(n_claims):
                # v13 format: type(int) + length(int) + value(long)
                rdr.read_int(); rdr.read_int(); rdr.read_long()
            _ = rdr.read_string()  # application_name
            responses.start_response(0).add_int(101)  # open_id
            return None

        if cmd == int(Command.CLOSE_DEVICE):
            _ = rdr.read_int()
            responses.start_response(0)
            return None

        if cmd == int(Command.LOCK_DEVICE):
            _ = rdr.read_int()
            _ = rdr.read_int()
            responses.start_response(0)
            return None

        if cmd == int(Command.UNLOCK_DEVICE):
            _ = rdr.read_int()
            responses.start_response(0)
            return None

        if cmd == int(Command.ACCESS_IR):
            _ = rdr.read_int()  # open_id
            instr = rdr.read_int()
            flags = rdr.read_int()
            r = responses.start_response(0)
            if flags & IR_FLAG_CAPTURE:
                r.add_int(instr ^ 0xAAAA)  # arbitrary but observable
            return None

        if cmd == int(Command.ACCESS_DR):
            _ = rdr.read_int()  # open_id
            length_dr = rdr.read_int()
            flags = rdr.read_int()
            _ = rdr.read_int()  # write_offset
            _ = rdr.read_int()  # write_length
            _ = rdr.read_int()  # read_offset
            read_length = rdr.read_int()
            if rdr.remaining >= 4:
                rdr.read_int()  # batch (>= v5)
            responses.start_response(0)
            if read_length > 0:
                return self.dr_responses.get(
                    read_length,
                    b"\xa5" * ((read_length + 7) // 8))
            return None

        if cmd == int(Command.RUN_TEST_IDLE):
            _ = rdr.read_int()
            _ = rdr.read_int()
            if rdr.remaining >= 4:
                rdr.read_int()  # flags (v5+)
            responses.start_response(0)
            return None

        if cmd == int(Command.TEST_LOGIC_RESET):
            _ = rdr.read_int()
            responses.start_response(0)
            return None

        responses.start_response(45)  # INVALID_PARAMETER
        return None

    @staticmethod
    def _encode_hardware_records(records):
        out = bytearray()
        for hw in records:
            out.extend(struct.pack(">I", hw["chain_id"]))
            name = hw["hw_name"].encode("latin-1")
            out.append(len(name)); out.extend(name)
            port = hw["port"].encode("latin-1")
            out.append(len(port)); out.extend(port)
            out.extend(struct.pack(">I", hw["chain_type"]))
            dn = hw.get("device_name", "").encode("latin-1")
            out.append(len(dn)); out.extend(dn)
            out.extend(struct.pack(">I", hw["features"]))
        return bytes(out)

    @staticmethod
    def _encode_device_records(records):
        out = bytearray()
        for dev in records:
            out.extend(struct.pack(">I", dev["idcode"]))
            out.extend(struct.pack(">I", dev["irlen"]))
            out.extend(struct.pack(">I", dev["features"]))
            out.extend(b"\x00" * 8)
            name = dev["name"].encode("latin-1")
            out.append(len(name)); out.extend(name)
        return bytes(out)


# --- fixtures --------------------------------------------------------------


@pytest.fixture
async def fake_server_and_client():
    server = _FakeServer(
        hardwares=[
            dict(chain_id=131073, hw_name="Fake-USB-Blaster",
                 port="FK001", chain_type=0,
                 device_name="", features=0x802),
        ],
        devices=[
            dict(idcode=0x4ba00477, irlen=4, features=0x04, name="ARM-DAP"),
        ],
    )
    asyncio_server = await asyncio.start_server(
        server.serve, host="127.0.0.1", port=0)
    port = asyncio_server.sockets[0].getsockname()[1]
    try:
        client = await AjiClient.connect("127.0.0.1", port)
        try:
            yield client, server
        finally:
            await client.close()
    finally:
        asyncio_server.close()
        await asyncio_server.wait_closed()


# --- tests ----------------------------------------------------------------


class TestNegotiation:
    @pytest.mark.asyncio
    async def test_handshake_picks_server_version(self, fake_server_and_client):
        client, _ = fake_server_and_client
        assert client.server_version == AJI_CURRENT_VERSION
        assert "fake jtagd" in client.server_version_info


class TestHardware:
    @pytest.mark.asyncio
    async def test_get_hardware(self, fake_server_and_client):
        client, _ = fake_server_and_client
        hws = await client.get_hardware()
        assert len(hws) == 1
        assert hws[0].chain_id == 131073
        assert hws[0].hw_name == "Fake-USB-Blaster"
        assert hws[0].port == "FK001"
        assert hws[0].chain_type == 0
        assert hws[0].features == 0x802


class TestChainOps:
    @pytest.mark.asyncio
    async def test_lock_unlock(self, fake_server_and_client):
        client, srv = fake_server_and_client
        await client.lock_chain(131073, 1000)
        await client.unlock_chain(131073)
        cmds = [c[0] for c in srv.commands]
        assert int(Command.LOCK_CHAIN) in cmds
        assert int(Command.UNLOCK_CHAIN) in cmds

    @pytest.mark.asyncio
    async def test_set_get_parameter(self, fake_server_and_client):
        client, srv = fake_server_and_client
        await client.set_parameter(131073, "JtagClock", 15_000_000)
        assert srv.parameters[(131073, "JtagClock")] == 15_000_000
        value = await client.get_parameter(131073, "JtagClock")
        assert value == 15_000_000

    @pytest.mark.asyncio
    async def test_scan_chain(self, fake_server_and_client):
        client, _ = fake_server_and_client
        devs = await client.scan_chain(131073)
        assert len(devs) == 1
        assert devs[0].idcode == 0x4ba00477
        assert devs[0].irlen == 4
        assert devs[0].name == "ARM-DAP"


class TestDeviceOps:
    @pytest.mark.asyncio
    async def test_open_close(self, fake_server_and_client):
        client, _ = fake_server_and_client
        oid = await client.open_device(131073, 0)
        assert oid == 101
        await client.close_device(oid)

    @pytest.mark.asyncio
    async def test_lock_unlock_device(self, fake_server_and_client):
        client, _ = fake_server_and_client
        oid = await client.open_device(131073, 0)
        await client.lock_device(oid, 1000)
        await client.unlock_device(oid)


class TestJtagOps:
    @pytest.mark.asyncio
    async def test_access_ir_no_capture(self, fake_server_and_client):
        client, srv = fake_server_and_client
        oid = await client.open_device(131073, 0)
        result = await client.access_ir(oid, 0x05)
        assert result is None
        # The cmd block should be 16 bytes total (header + 3 ints).
        ir_payload = next(c for c in srv.commands
                          if c[0] == int(Command.ACCESS_IR))[1]
        # Find our ACCESS_IR block within the request payload.
        rdr = MessageReader(ir_payload)
        while rdr.next_block() != int(Command.ACCESS_IR):
            pass
        assert rdr.read_int() == 101   # open_id
        assert rdr.read_int() == 0x05  # instruction
        assert rdr.read_int() == 0     # flags

    @pytest.mark.asyncio
    async def test_access_ir_with_capture(self, fake_server_and_client):
        client, _ = fake_server_and_client
        oid = await client.open_device(131073, 0)
        result = await client.access_ir(oid, 0x05, IR_FLAG_CAPTURE)
        assert result == 0x05 ^ 0xAAAA

    @pytest.mark.asyncio
    async def test_access_dr_capture(self, fake_server_and_client):
        client, srv = fake_server_and_client
        srv.dr_responses[8] = b"\x42"
        oid = await client.open_device(131073, 0)
        out = await client.access_dr(oid, length_dr=8,
                                     write_bits=b"\x00",
                                     flags=DR_FLAG_CAPTURE)
        assert out == b"\x42"

    @pytest.mark.asyncio
    async def test_access_dr_write_only(self, fake_server_and_client):
        client, srv = fake_server_and_client
        oid = await client.open_device(131073, 0)
        out = await client.access_dr(oid, length_dr=16,
                                     write_bits=b"\xab\xcd")
        assert out == b""
        # Server should have received the FIFO bytes.
        assert srv.pending_fifo[-1] == b"\xab\xcd"

    @pytest.mark.asyncio
    async def test_run_test_idle(self, fake_server_and_client):
        client, srv = fake_server_and_client
        oid = await client.open_device(131073, 0)
        await client.run_test_idle(oid, 16)
        assert any(c[0] == int(Command.RUN_TEST_IDLE) for c in srv.commands)

    @pytest.mark.asyncio
    async def test_test_logic_reset(self, fake_server_and_client):
        client, srv = fake_server_and_client
        oid = await client.open_device(131073, 0)
        await client.test_logic_reset(oid)
        assert any(c[0] == int(Command.TEST_LOGIC_RESET) for c in srv.commands)
