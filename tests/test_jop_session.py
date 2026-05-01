"""End-to-end test: a fake Quartus-side client speaks the JoP wire
protocol against a real :class:`JopListener` driving a recording
:class:`JtagInterface`.

This validates the 5-socket handshake, control-plane PING/GET_PARAM,
and the H2T → walker → T2H data path including TDO byte packing.
"""

import asyncio
import re

import pytest

from acrobe.bitstring import BitString
from acrobe.protocol import jtag
from acrobe.jop import bytestream as bs
from acrobe.jop import control, framing
from acrobe.jop.listener import JopListener


class _RecordingInterface(jtag.JtagInterface):
    """Synthesises TDO = ~tdi for shifts; records ops for assertions."""

    def __init__(self):
        super().__init__()
        self.ops: list = []

    async def flush_ops(self, batch):
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, jtag.Shift):
                inv = (1 << len(op.tdi)) - 1 - int(op.tdi)
                op.tdo = BitString(inv, len(op.tdi))
            future.set_result(op)


@pytest.fixture
async def server_and_client():
    iface = _RecordingInterface()
    listener = JopListener(iface, host="127.0.0.1", port=0)
    server_task = asyncio.create_task(listener.serve_forever())
    # Wait for the listener to bind.
    for _ in range(50):
        try:
            port = listener.server_port
            break
        except RuntimeError:
            await asyncio.sleep(0.01)
    else:
        raise RuntimeError("server never bound")
    try:
        yield iface, listener, port
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


async def _readuntil_nul(reader: asyncio.StreamReader) -> bytes:
    data = await reader.readuntil(b"\0")
    return data[:-1]


async def _do_handshake(port: int) -> dict[str, tuple[asyncio.StreamReader,
                                                       asyncio.StreamWriter]]:
    """Walk through the 5-socket handshake as a client."""
    sockets: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
    # CTRL first.
    r, w = await asyncio.open_connection("127.0.0.1", port)
    welcome = await _readuntil_nul(r)
    m = re.search(rb"HANDLE=(\d+)", welcome)
    assert m
    handle = int(m.group(1))
    w.write(control.expected_handle_message(control.SOCK_CONTROL, handle))
    await w.drain()
    ack = await _readuntil_nul(r)
    assert ack == b"READY"
    sockets["ctrl"] = (r, w)

    for slot, name in [("mgmt", control.SOCK_MGMT),
                       ("mgmt_rsp", control.SOCK_MGMT_RSP),
                       ("h2t", control.SOCK_H2T),
                       ("t2h", control.SOCK_T2H)]:
        cr, cw = await asyncio.open_connection("127.0.0.1", port)
        cw.write(control.expected_handle_message(name, handle))
        await cw.drain()
        ack = await _readuntil_nul(cr)
        assert ack == b"READY"
        sockets[slot] = (cr, cw)

    # Final READY on CTRL.
    final = await _readuntil_nul(sockets["ctrl"][0])
    assert final == b"READY"
    return sockets


async def _close_all(sockets):
    for r, w in sockets.values():
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass


class TestHandshake:
    @pytest.mark.asyncio
    async def test_full_handshake(self, server_and_client):
        _, _, port = server_and_client
        sockets = await _do_handshake(port)
        await _close_all(sockets)

    @pytest.mark.asyncio
    async def test_second_client_rejected(self, server_and_client):
        _, _, port = server_and_client
        sockets = await _do_handshake(port)
        # Second CTRL connection should get SERVER_BUSY.
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        msg = await _readuntil_nul(r2)
        assert msg == b"SERVER_BUSY"
        w2.close()
        await _close_all(sockets)


class TestControlPlane:
    @pytest.mark.asyncio
    async def test_ping(self, server_and_client):
        _, _, port = server_and_client
        sockets = await _do_handshake(port)
        cr, cw = sockets["ctrl"]
        cw.write(b"PING\0")
        await cw.drain()
        rsp = await _readuntil_nul(cr)
        assert rsp == b"PONG"
        await _close_all(sockets)

    @pytest.mark.asyncio
    async def test_get_param_mgmt_support_zero(self, server_and_client):
        _, _, port = server_and_client
        sockets = await _do_handshake(port)
        cr, cw = sockets["ctrl"]
        cw.write(b"GET_PARAM MGMT_SUPPORT\0")
        await cw.drain()
        rsp = await _readuntil_nul(cr)
        assert rsp == b"0"
        await _close_all(sockets)

    @pytest.mark.asyncio
    async def test_unknown_command(self, server_and_client):
        _, _, port = server_and_client
        sockets = await _do_handshake(port)
        cr, cw = sockets["ctrl"]
        cw.write(b"FROBNICATE\0")
        await cw.drain()
        rsp = await _readuntil_nul(cr)
        assert rsp == b"UNRECOGNIZED_COMMAND"
        await _close_all(sockets)

    @pytest.mark.asyncio
    async def test_set_driver_param_hw_loopback(self, server_and_client):
        _, _, port = server_and_client
        sockets = await _do_handshake(port)
        cr, cw = sockets["ctrl"]
        cw.write(b"SET_DRIVER_PARAM #HW_LOOPBACK 1\0")
        await cw.drain()
        rsp = await _readuntil_nul(cr)
        assert rsp == b"SET_PARAM_ACK"
        # And we can read it back.
        cw.write(b"GET_DRIVER_PARAM #HW_LOOPBACK\0")
        await cw.drain()
        rsp = await _readuntil_nul(cr)
        assert rsp == b"1"
        await _close_all(sockets)

    @pytest.mark.asyncio
    async def test_disconnect_closes_session(self, server_and_client):
        _, listener, port = server_and_client
        sockets = await _do_handshake(port)
        cr, cw = sockets["ctrl"]
        cw.write(b"DISCONNECT\0")
        await cw.drain()
        rsp = await _readuntil_nul(cr)
        assert rsp == b"DISCONNECT_ACK"
        # Server should now be free for a new client.
        for _ in range(50):
            if not listener.busy:
                break
            await asyncio.sleep(0.02)
        await _close_all(sockets)
        assert not listener.busy


def _bits_to_bs(bits):
    out = BitString()
    for b in bits:
        out += BitString(b, 1)
    return out


class TestDataPlane:
    @pytest.mark.asyncio
    async def test_retrieve_info_returns_zero(self, server_and_client):
        _, _, port = server_and_client
        sockets = await _do_handshake(port)

        # Send a single-byte H2T packet containing CMD_CONFIG / RETRIEVE_INFO.
        h2t_payload = bs.encode_retrieve_info()
        pkt = framing.H2tPacket(sop=True, eop=True, conn_id=0, channel=0,
                                  payload=h2t_payload)
        sockets["h2t"][1].write(pkt.encode())
        await sockets["h2t"][1].drain()

        # Server should reply with one T2H packet carrying 0x00.
        rsp = await framing.read_h2t_packet(sockets["t2h"][0])
        assert rsp.payload == bytes([bs.CONFIG_INFO_RESPONSE_BYTE])

        await _close_all(sockets)

    @pytest.mark.asyncio
    async def test_hw_loopback_echoes_h2t_to_t2h(self, server_and_client):
        # Mirror Intel's remote_debug_tester_app flow: enable HW
        # loopback, fire arbitrary bytes (NOT a valid JoP byte stream)
        # and expect them echoed back verbatim.
        iface, _, port = server_and_client
        sockets = await _do_handshake(port)
        cr, cw = sockets["ctrl"]
        cw.write(b"SET_DRIVER_PARAM #HW_LOOPBACK 1\0")
        await cw.drain()
        assert await _readuntil_nul(cr) == b"SET_PARAM_ACK"

        # Random non-JoP payload — would crash the decoder if we routed
        # it through normal processing.
        payload = bytes(range(49))
        pkt = framing.H2tPacket(sop=True, eop=True, conn_id=7,
                                  channel=42, payload=payload)
        sockets["h2t"][1].write(pkt.encode())
        await sockets["h2t"][1].drain()

        rsp = await framing.read_h2t_packet(sockets["t2h"][0])
        # Echoed verbatim — payload, channel, conn_id, sop, eop preserved.
        assert rsp.payload == payload
        assert rsp.channel == 42
        assert rsp.conn_id == 7
        assert rsp.sop is True
        assert rsp.eop is True
        # Loopback path bypasses the JTAG interface entirely.
        assert iface.ops == []

        await _close_all(sockets)

    @pytest.mark.asyncio
    async def test_h2t_decoder_error_doesnt_kill_session(self, server_and_client):
        # If the H2T payload is malformed (not a valid JoP byte stream)
        # and loopback is OFF, the session must not crash — it should
        # log, reset the decoder, and stay alive for follow-up traffic.
        _, _, port = server_and_client
        sockets = await _do_handshake(port)

        bad = framing.H2tPacket(sop=True, eop=True, conn_id=0, channel=0,
                                  payload=bytes([0xE0]))  # opcode 0b111 unused
        sockets["h2t"][1].write(bad.encode())
        await sockets["h2t"][1].drain()

        # Session is still alive — control plane still answers.
        cr, cw = sockets["ctrl"]
        cw.write(b"PING\0")
        await cw.drain()
        rsp = await _readuntil_nul(cr)
        assert rsp == b"PONG"

        await _close_all(sockets)

    @pytest.mark.asyncio
    async def test_shift_drives_walker_and_returns_t2h(self, server_and_client):
        iface, _, port = server_and_client
        sockets = await _do_handshake(port)

        # Build an end-to-end JoP byte stream:
        #   - schedule a 10-tick capture window covering the full shift
        #     command we're about to send;
        #   - send a CMD_SHORT_CUSTOM_TMS_TDI(10 bits) that walks
        #     TLR→RTI→Sel-DR→Cap-DR→Shift-DR (5 bits of shift incl.
        #     boundary)→Update→RTI.
        #
        # We don't strictly assert the byte contents here — the wire-level
        # TCK count Quartus expects vs. the actual TCKs acrobe emits
        # diverges around state transitions, and exact alignment will be
        # validated against real Quartus captures. What we *do* assert
        # is that:
        #   (1) the interface saw the expected acrobe ops;
        #   (2) a T2H response packet comes back with the right framing.
        tms_walk = _bits_to_bs([0, 1, 0, 0, 0, 0, 0, 1, 1, 0])
        tdi_walk = _bits_to_bs([0, 0, 0, 1, 0, 1, 0, 0, 0, 0])

        h2t_payload = (
            bs.encode_write_tdo_enable_fifo(
                duration=10, tdo_enable=True, eop_gen=True)
            + bs.encode_short_custom_tms_tdi(tms_walk, tdi_walk)
        )
        pkt = framing.H2tPacket(sop=True, eop=True, conn_id=0, channel=0,
                                  payload=h2t_payload)
        sockets["h2t"][1].write(pkt.encode())
        await sockets["h2t"][1].drain()

        rsp = await framing.read_h2t_packet(sockets["t2h"][0])
        # 10 bits of capture → 2 bytes (LSB-first).
        assert len(rsp.payload) == 2
        assert rsp.eop is True

        kinds = [type(op).__name__ for op in iface.ops]
        # Reset (TLR→RTI), Run(1), CaptureDr, Shift, Run(1).
        assert kinds == ["Reset", "Run", "CaptureDr", "Shift", "Run"]
        # The shift segment covers the actual shift bits only (4 of them
        # — bits 4..7 of the input, with bit 7 being the TMS=1 boundary).
        # The Cap-DR→Shift-DR entry edge (bit 3) is NOT a shift action
        # and must not be forwarded to acrobe.
        assert len(iface.ops[3].tdi) == 4

        await _close_all(sockets)
