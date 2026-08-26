"""Tests for the JoP byte-stream codec."""

import pytest

from acrobe.bitstring import BitString
from acrobe.jop import bytestream as bs


def _bits_to_bs(bits):
    out = BitString()
    for b in bits:
        out += BitString(b, 1)
    return out


class TestConfig:
    def test_retrieve_info_round_trip(self):
        wire = bs.encode_retrieve_info()
        assert wire == bytes([0x00])
        ops = bs.JopDecoder().feed(wire)
        assert len(ops) == 1
        assert isinstance(ops[0], bs.RetrieveInfo)

    def test_reset_fifo_round_trip(self):
        wire = bs.encode_reset_tdo_fifo()
        assert wire == bytes([0x01])
        ops = bs.JopDecoder().feed(wire)
        assert len(ops) == 1
        assert isinstance(ops[0], bs.ResetTdoFifo)

    def test_response_byte_constant(self):
        # The HDL hard-codes VERSION=0 and RESERVED=0, so the response
        # is identically zero.
        assert bs.CONFIG_INFO_RESPONSE_BYTE == 0x00


class TestShortCustomTmsTdi:
    def test_single_bit(self):
        tms = _bits_to_bs([1])
        tdi = _bits_to_bs([0])
        wire = bs.encode_short_custom_tms_tdi(tms, tdi)
        # opcode=0b100, count-1=0 → 0x80; data byte: tms_lsb=1, tdi_lsb=0 → 0x10
        assert wire == bytes([0x80, 0x10])
        ops = bs.JopDecoder().feed(wire)
        assert len(ops) == 1 and isinstance(ops[0], bs.Shift)
        assert ops[0].num_bits == 1
        assert int(ops[0].tms) == 1
        assert int(ops[0].tdi) == 0

    def test_round_trip_8_bits(self):
        # 8 bits cover 2 nibbles → 2 data bytes.
        tms = _bits_to_bs([0, 1, 1, 0, 1, 0, 0, 1])
        tdi = _bits_to_bs([1, 0, 1, 1, 0, 0, 1, 1])
        wire = bs.encode_short_custom_tms_tdi(tms, tdi)
        assert len(wire) == 1 + 2  # header + 2 nibble bytes
        ops = bs.JopDecoder().feed(wire)
        assert len(ops) == 1
        op = ops[0]
        assert op.num_bits == 8
        assert op.tms == tms
        assert op.tdi == tdi

    def test_max_size_32_bits(self):
        tms = _bits_to_bs([(i & 1) for i in range(32)])
        tdi = _bits_to_bs([((i >> 1) & 1) for i in range(32)])
        wire = bs.encode_short_custom_tms_tdi(tms, tdi)
        # length field encoded as 31 = 0x1F → header byte 0x9F
        assert wire[0] == 0x9F
        ops = bs.JopDecoder().feed(wire)
        assert ops[0].num_bits == 32
        assert ops[0].tms == tms
        assert ops[0].tdi == tdi

    def test_size_above_max_raises(self):
        with pytest.raises(ValueError):
            bs.encode_short_custom_tms_tdi(BitString(0, 33), BitString(0, 33))


class TestLongFixedTmsCustomTdi:
    def test_round_trip_byte_aligned(self):
        tdi = BitString(b"\xa5\x12\x34", 24)
        wire = bs.encode_long_fixed_tms_custom_tdi(tdi)
        # opcode=0b101, length-1=23 → header low5 = 23 = 0x17 → first byte 0xB7,
        # second byte (length-1)>>5 = 0
        assert wire[0] == 0xB7
        assert wire[1] == 0x00
        assert wire[2:] == b"\xa5\x12\x34"
        ops = bs.JopDecoder().feed(wire)
        assert len(ops) == 1
        assert ops[0].num_bits == 24
        assert ops[0].tms == BitString(0, 24)
        assert ops[0].tdi == tdi

    def test_max_size(self):
        tdi = BitString(0, bs.LONG_MAX_BITS)
        wire = bs.encode_long_fixed_tms_custom_tdi(tdi)
        ops = bs.JopDecoder().feed(wire)
        assert ops[0].num_bits == bs.LONG_MAX_BITS

    def test_above_max_raises(self):
        with pytest.raises(ValueError):
            bs.encode_long_fixed_tms_custom_tdi(BitString(0, bs.LONG_MAX_BITS + 1))


class TestLongFixedTmsTdi:
    def test_idle_ticks(self):
        wire = bs.encode_long_fixed_tms_tdi(100)
        ops = bs.JopDecoder().feed(wire)
        assert len(ops) == 1
        assert ops[0].num_bits == 100
        assert ops[0].tms == BitString(0, 100)
        assert ops[0].tdi == BitString(0, 100)


class TestWriteTdoEnableFifo:
    def test_round_trip(self):
        wire = bs.encode_write_tdo_enable_fifo(
            duration=300, tdo_enable=True, eop_gen=False)
        ops = bs.JopDecoder().feed(wire)
        assert len(ops) == 1
        op = ops[0]
        assert isinstance(op, bs.PushTdoCapture)
        assert op.duration == 300
        assert op.tdo_enable is True
        assert op.eop_gen is False

    def test_eop_gen_set(self):
        wire = bs.encode_write_tdo_enable_fifo(
            duration=8, tdo_enable=True, eop_gen=True)
        op = bs.JopDecoder().feed(wire)[0]
        assert op.eop_gen is True

    def test_max_duration(self):
        d = 1 << 15
        op = bs.JopDecoder().feed(
            bs.encode_write_tdo_enable_fifo(d, tdo_enable=False, eop_gen=False))[0]
        assert op.duration == d


class TestStreamingDecoder:
    def test_split_across_feeds(self):
        # Encode three commands then drip-feed bytes one at a time.
        wire = (
            bs.encode_retrieve_info()
            + bs.encode_short_custom_tms_tdi(_bits_to_bs([1, 0, 1]),
                                              _bits_to_bs([0, 1, 1]))
            + bs.encode_long_fixed_tms_tdi(8)
        )
        decoder = bs.JopDecoder()
        ops_collected = []
        for i in range(len(wire)):
            ops_collected += decoder.feed(wire[i:i + 1])
        assert len(ops_collected) == 3
        assert isinstance(ops_collected[0], bs.RetrieveInfo)
        assert isinstance(ops_collected[1], bs.Shift)
        assert ops_collected[1].num_bits == 3
        assert isinstance(ops_collected[2], bs.Shift)
        assert ops_collected[2].num_bits == 8


class TestEncoder:
    def test_lsb_first_packing(self):
        enc = bs.JopEncoder()
        # 8 captured bits = 0b10110010 (bit0 first → byte = 0x4D when
        # packed LSB-first: 1,0,1,1,0,0,1,0 → byte = 0b01001101).
        bits = _bits_to_bs([1, 0, 1, 1, 0, 0, 1, 0])
        out, eop = enc.emit_window(bits)
        assert out == bytes([0b01001101])
        assert eop == []

    def test_partial_byte_held(self):
        enc = bs.JopEncoder()
        out, _ = enc.emit_window(_bits_to_bs([1, 1, 0]))
        assert out == b""  # partial byte not flushed
        out, _ = enc.emit_window(_bits_to_bs([1, 0]))
        assert out == b""
        # 5 bits buffered + 3 more = 8 → flush.
        # Total bits in order: 1,1,0,1,0,1,1,0 (LSB first) = 0b01101011.
        out, _ = enc.emit_window(_bits_to_bs([1, 1, 0]))
        assert out == bytes([0b01101011])

    def test_eop_flushes_partial_byte_and_marks_it(self):
        enc = bs.JopEncoder()
        out, eop = enc.emit_window(_bits_to_bs([1, 0, 1]), eop=True)
        # Partial byte flushed because eop.
        assert out == bytes([0b00000101])
        assert eop == [0]
