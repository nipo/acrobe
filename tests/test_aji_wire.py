"""Tests for the libaji-faithful AJI wire layer.

The byte literals here come from a transparent-proxy capture of a real
``jtagd v13`` from Quartus 25.3.1, so passing tests here demonstrate
we encode/decode the same bytes Intel's reference implementation does.
"""

import struct

import pytest

from acrobe.adapter.aji.wire import (
    AJI_CURRENT_VERSION, AJI_SIGNATURE,
    Command, Greeting,
    MUX_COMMAND, MUX_FIFO_MIN,
    MessageBuilder, MessageReader,
    build_greeting, decode_mux_header, encode_mux_header,
    encode_mux_packet, parse_greeting,
)


class TestMuxHeader:
    def test_command_short(self):
        # mux=0, len=4. libaji header = (0 << 12) | (4-1) = 0x0003.
        assert encode_mux_header(0, 4) == b"\x00\x03"

    def test_command_max_short(self):
        # Largest payload that still keeps the high byte 0x00.
        assert encode_mux_header(0, 256) == b"\x00\xff"

    def test_command_long(self):
        # mux=0, len=257 → header = 0x0100.
        assert encode_mux_header(0, 257) == b"\x01\x00"

    def test_command_max(self):
        # Largest payload allowed: 4096. header = 0x0FFF.
        assert encode_mux_header(0, 4096) == b"\x0f\xff"

    def test_fifo_short(self):
        # mux=4 (first FIFO), len=8 → header = (4<<12) | 7 = 0x4007.
        assert encode_mux_header(4, 8) == b"\x40\x07"

    def test_fifo_long(self):
        # mux=8, len=2000 → header = (8<<12) | 1999 = 0x87CF.
        assert encode_mux_header(8, 2000) == b"\x87\xcf"

    def test_decode_round_trip(self):
        for mux in range(16):
            for length in (1, 7, 256, 257, 1024, 4096):
                header = encode_mux_header(mux, length)
                d_mux, d_len = decode_mux_header(header)
                assert d_mux == mux
                assert d_len == length

    def test_oversize_payload_rejected(self):
        with pytest.raises(ValueError):
            encode_mux_header(0, 4097)

    def test_zero_payload_rejected(self):
        with pytest.raises(ValueError):
            encode_mux_header(0, 0)

    def test_invalid_mux_rejected(self):
        with pytest.raises(ValueError):
            encode_mux_header(16, 4)


class TestMessageBuilder:
    def test_single_block(self):
        msg = MessageBuilder().add_command(Command.PING).build()
        # cmd=0xFD, reserved=0, total_len=4 (just the header)
        assert msg == b"\xfd\x00\x00\x04"

    def test_block_with_int(self):
        msg = (MessageBuilder()
               .add_command(Command.USE_PROTOCOL_VERSION)
               .add_int(13)
               .build())
        # cmd=0xFE, reserved=0, total_len=8, payload=int(13)
        assert msg == b"\xfe\x00\x00\x08\x00\x00\x00\x0d"

    def test_two_blocks_concatenated(self):
        msg = (MessageBuilder()
               .add_command(Command.USE_PROTOCOL_VERSION)
               .add_int(13)
               .add_command(Command.GET_VERSION_INFO)
               .build())
        assert msg == (
            b"\xfe\x00\x00\x08\x00\x00\x00\x0d"
            b"\x83\x00\x00\x04"
        )

    def test_response_with_string_payload(self):
        msg = (MessageBuilder()
               .start_response(0)
               .add_int(1)
               .add_string("JtagClock")
               .build())
        # status=0, reserved=0, total_len = 4 + 4 + 1 + 9 = 18
        expected = (b"\x00\x00\x00\x12"
                    b"\x00\x00\x00\x01"
                    b"\x09JtagClock")
        assert msg == expected


class TestMessageReader:
    def test_read_single_block(self):
        rdr = MessageReader(b"\xfe\x00\x00\x08\x00\x00\x00\x0d")
        cmd = rdr.next_block()
        assert cmd == int(Command.USE_PROTOCOL_VERSION)
        assert rdr.read_int() == 13
        assert rdr.remaining == 0
        assert rdr.at_end

    def test_read_two_blocks(self):
        data = (b"\xfe\x00\x00\x08\x00\x00\x00\x0d"
                b"\x83\x00\x00\x04")
        rdr = MessageReader(data)
        assert rdr.next_block() == int(Command.USE_PROTOCOL_VERSION)
        assert rdr.read_int() == 13
        assert rdr.next_block() == int(Command.GET_VERSION_INFO)
        assert rdr.remaining == 0
        assert rdr.at_end

    def test_string_round_trip(self):
        msg = (MessageBuilder()
               .start_response(0)
               .add_string("Version 25.3.1 SC Pro Edition")
               .build())
        rdr = MessageReader(msg)
        rdr.next_block()
        assert rdr.read_string() == "Version 25.3.1 SC Pro Edition"

    def test_truncated_block_raises(self):
        rdr = MessageReader(b"\xfe\x00\x00")  # < 4 header bytes
        with pytest.raises(EOFError):
            rdr.next_block()

    def test_block_extends_past_buffer(self):
        # total_len=10 but only 4 bytes follow
        rdr = MessageReader(b"\xfe\x00\x00\x0a\x00\x00\x00\x00")
        with pytest.raises(ValueError):
            rdr.next_block()


class TestGreeting:
    def test_parse_jtagd_greeting(self):
        # signature 0x0d "JTAG Server\r\n" + version=13 + authtype=0
        # The greeting payload is everything *after* the 4-byte block header.
        payload = (
            bytes([len(AJI_SIGNATURE)])
            + AJI_SIGNATURE.encode("latin-1")
            + struct.pack(">I", 13)
            + struct.pack(">I", 0)
        )
        g = parse_greeting(payload)
        assert g.server_version == 13
        assert g.authtype == 0
        assert g.challenge is None

    def test_parse_with_md5_challenge(self):
        challenge = b"\x01" * 16
        payload = (
            bytes([len(AJI_SIGNATURE)])
            + AJI_SIGNATURE.encode("latin-1")
            + struct.pack(">I", 13)
            + struct.pack(">I", int(Command.AUTHENTICATE_MD5))
            + challenge
        )
        g = parse_greeting(payload)
        assert g.server_version == 13
        assert g.authtype == int(Command.AUTHENTICATE_MD5)
        assert g.challenge == challenge

    def test_round_trip_no_auth(self):
        payload = build_greeting(13)
        g = parse_greeting(payload)
        assert g.server_version == 13
        assert g.authtype == 0
        assert g.challenge is None

    def test_round_trip_with_auth(self):
        challenge = bytes(range(16))
        payload = build_greeting(13,
                                 authtype=int(Command.AUTHENTICATE_MD5),
                                 challenge=challenge)
        g = parse_greeting(payload)
        assert g.challenge == challenge

    def test_wrong_signature_rejected(self):
        bogus_sig = "Wrong Banner\r\n"
        payload = (
            bytes([len(bogus_sig)])
            + bogus_sig.encode("latin-1")
            + struct.pack(">I", 13)
            + struct.pack(">I", 0)
        )
        with pytest.raises(ValueError, match="signature"):
            parse_greeting(payload)


class TestVersionInitHandshake:
    """Reproduce what the captured jtagd traffic shows libaji clients
    sending after the greeting: a single mux packet on channel 0
    containing two batched commands USE_PROTOCOL_VERSION + GET_VERSION_INFO.
    """

    def test_handshake_message_layout(self):
        body = (MessageBuilder()
                .add_command(Command.USE_PROTOCOL_VERSION)
                .add_int(13)
                .add_command(Command.GET_VERSION_INFO)
                .build())
        packet = encode_mux_packet(MUX_COMMAND, body)
        # Mux header = 12 bits of (12 - 1) = 0x00b → bytes 00 0b
        assert packet[:2] == b"\x00\x0b"
        assert packet[2:] == body
        # Total bytes is 14 (2 mux header + 12 body bytes).
        assert len(packet) == 14
