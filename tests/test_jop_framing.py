"""Tests for the Avalon-ST-over-TCP packet framing."""

import asyncio
import io

import pytest

from acrobe.jop import framing


class _BytesReader:
    """asyncio.StreamReader-like wrapper around a bytes buffer."""

    def __init__(self, data: bytes):
        self._buf = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self._pos + n > len(self._buf):
            raise asyncio.IncompleteReadError(self._buf[self._pos:], n)
        chunk = self._buf[self._pos:self._pos + n]
        self._pos += n
        return chunk


class TestH2tEncodeDecode:
    def test_round_trip_minimal(self):
        pkt = framing.H2tPacket(
            sop=True, eop=True, conn_id=0x42, channel=0x123,
            payload=b"hello")
        wire = pkt.encode()
        # 4 guardband + 6 header + 5 payload = 15 bytes
        assert len(wire) == 4 + 6 + 5
        assert wire[:4] == framing.GUARDBAND
        # flags: 0x03; conn_id: 0x42; channel LE: 0x23 0x01;
        # data_len LE: 0x05 0x00
        assert wire[4:10] == bytes([0x03, 0x42, 0x23, 0x01, 0x05, 0x00])
        assert wire[10:] == b"hello"

    def test_decode_header_extracts_fields(self):
        hdr = bytes([0x03, 0x42, 0x23, 0x01, 0x05, 0x00])
        sop, eop, conn_id, channel, data_len = framing.H2tPacket.decode_header(hdr)
        assert (sop, eop, conn_id, channel, data_len) == (
            True, True, 0x42, 0x123, 5)

    def test_channel_masked_to_11_bits(self):
        # On the wire, channel is a u16, but spec masks to 11 bits.
        hdr = bytes([0x00, 0x00, 0xFF, 0xFF, 0x00, 0x00])
        _, _, _, channel, _ = framing.H2tPacket.decode_header(hdr)
        assert channel == 0x07FF

    def test_payload_oversize_rejected(self):
        too_big = b"\x00" * (framing.H2T_MAX_PAYLOAD + 1)
        with pytest.raises(ValueError):
            framing.H2tPacket(sop=True, eop=True, conn_id=0,
                              channel=0, payload=too_big).encode()

    @pytest.mark.asyncio
    async def test_read_h2t_packet(self):
        original = framing.H2tPacket(
            sop=True, eop=False, conn_id=7, channel=42,
            payload=b"abcdef")
        reader = _BytesReader(original.encode())
        decoded = await framing.read_h2t_packet(reader)
        assert decoded == original

    @pytest.mark.asyncio
    async def test_bad_guardband_raises(self):
        bad = b"\x00\x00\x00\x00" + bytes([0x01, 0, 0, 0, 0, 0])
        with pytest.raises(framing.GuardbandError):
            await framing.read_h2t_packet(_BytesReader(bad))


class TestMgmtEncodeDecode:
    def test_round_trip(self):
        pkt = framing.MgmtPacket(
            sop=True, eop=True, channel=5, payload=b"\x01\x02\x03")
        wire = pkt.encode()
        assert wire[:4] == framing.GUARDBAND
        # MGMT byte 1 is reserved/zero
        assert wire[5] == 0
        # channel LE: 0x05 0x00
        assert wire[6:8] == bytes([0x05, 0x00])

    def test_payload_can_be_64k_minus_1(self):
        big = b"\x00" * framing.MGMT_MAX_PAYLOAD
        wire = framing.MgmtPacket(sop=True, eop=True,
                                   channel=0, payload=big).encode()
        # 4 + 6 + len(big)
        assert len(wire) == 4 + 6 + framing.MGMT_MAX_PAYLOAD

    @pytest.mark.asyncio
    async def test_read_mgmt_packet(self):
        pkt = framing.MgmtPacket(sop=True, eop=True, channel=2,
                                  payload=b"xyz")
        decoded = await framing.read_mgmt_packet(_BytesReader(pkt.encode()))
        assert decoded == pkt
