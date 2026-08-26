import pytest
from acrobe.bitstring import BitString, BitStringSlice


class TestConstruction:
    def test_empty(self):
        bs = BitString()
        assert len(bs) == 0
        assert not bs
        assert int(bs) == 0
        assert str(bs) == "."

    def test_from_int(self):
        bs = BitString(0x1234, 16)
        assert len(bs) == 16
        assert int(bs) == 0x1234
        assert bytes(bs) == b'\x34\x12'

    def test_from_int_odd_bits(self):
        bs = BitString(0x1f, 5)
        assert len(bs) == 5
        assert int(bs) == 0x1f

    def test_from_bytes(self):
        bs = BitString(b'\xab\xcd')
        assert len(bs) == 16
        assert int(bs) == 0xcdab

    def test_from_bytes_with_length(self):
        bs = BitString(b'\xff', 4)
        assert len(bs) == 4
        assert int(bs) == 0xf

    def test_from_bitstring(self):
        a = BitString(0xaa, 8)
        b = BitString(a)
        assert a == b
        assert a is not b

    def test_negative_int(self):
        bs = BitString(-1, 8)
        assert int(bs) == 0xff

    def test_zero_length(self):
        bs = BitString(0, 0)
        assert len(bs) == 0
        assert not bs


class TestBool:
    def test_nonempty_is_true(self):
        assert bool(BitString(0, 8))

    def test_empty_is_false(self):
        assert not bool(BitString())


class TestIntConversion:
    def test_masks_to_length(self):
        # 7 bits: data byte is 0xff but only 7 bits valid
        bs = BitString(0x7f, 7)
        assert int(bs) == 0x7f

    def test_multi_byte(self):
        bs = BitString(0xdeadbeef, 32)
        assert int(bs) == 0xdeadbeef


class TestStr:
    def test_lsb_first(self):
        # 0x5 = 101 binary, LSB first => "101"
        bs = BitString(0x5, 3)
        assert str(bs) == "101"

    def test_byte(self):
        bs = BitString(0xa5, 8)
        # 0xa5 = 10100101, LSB first = "10100101"
        assert str(bs) == "10100101"


class TestEquality:
    def test_equal(self):
        assert BitString(0xaa, 8) == BitString(0xaa, 8)

    def test_not_equal_value(self):
        assert BitString(0xaa, 8) != BitString(0xbb, 8)

    def test_not_equal_length(self):
        assert BitString(0x0a, 4) != BitString(0x0a, 8)


class TestHash:
    def test_equal_hash(self):
        a = BitString(0x1234, 16)
        b = BitString(0x1234, 16)
        assert hash(a) == hash(b)

    def test_usable_in_set(self):
        s = {BitString(0xaa, 8), BitString(0xaa, 8)}
        assert len(s) == 1


class TestAppend:
    def test_byte_aligned(self):
        bs = BitString(0xaa, 8)
        bs.append(0xbb, 8)
        assert int(bs) == 0xbbaa
        assert len(bs) == 16

    def test_unaligned(self):
        bs = BitString(0x3, 2)
        bs.append(0x5, 3)
        assert len(bs) == 5
        # bits: 11 101 (LSB first)
        assert int(bs) == 0x3 | (0x5 << 2)

    def test_append_bitstring(self):
        a = BitString(0xaa, 8)
        b = BitString(0xbb, 8)
        a.append(b)
        assert len(a) == 16
        assert int(a) == 0xbbaa


class TestConcat:
    def test_add(self):
        a = BitString(0xaa, 8)
        b = BitString(0xbb, 8)
        c = a + b
        assert len(c) == 16
        assert int(c) == 0xbbaa
        # originals unchanged
        assert len(a) == 8
        assert len(b) == 8

    def test_iadd(self):
        a = BitString(0xaa, 8)
        a += BitString(0xbb, 8)
        assert len(a) == 16
        assert int(a) == 0xbbaa

    def test_unaligned_concat(self):
        a = BitString(0x7, 3)
        b = BitString(0x5, 3)
        c = a + b
        assert len(c) == 6
        assert int(c) == 0x7 | (0x5 << 3)


class TestGetItem:
    def test_single_bit(self):
        bs = BitString(0b10110, 5)
        assert bs[0] == False
        assert bs[1] == True
        assert bs[2] == True
        assert bs[3] == False
        assert bs[4] == True

    def test_negative_index(self):
        bs = BitString(0b1010, 4)
        assert bs[-1] == True
        assert bs[-2] == False

    def test_out_of_range(self):
        bs = BitString(0xff, 8)
        with pytest.raises(IndexError):
            bs[8]

    def test_slice_returns_bitstringslice(self):
        bs = BitString(0xabcd, 16)
        s = bs[4:12]
        assert isinstance(s, BitStringSlice)
        assert len(s) == 8

    def test_slice_value(self):
        bs = BitString(0xabcd, 16)
        s = bs[4:12]
        # bits 4..11 of 0xabcd
        assert int(s) == (0xabcd >> 4) & 0xff

    def test_slice_negative(self):
        bs = BitString(0xff, 8)
        s = bs[-4:]
        assert len(s) == 4
        assert int(s) == 0xf

    def test_empty_slice(self):
        bs = BitString(0xff, 8)
        s = bs[4:4]
        assert len(s) == 0


class TestSetItemSingleBit:
    def test_set_bit(self):
        bs = BitString(0, 8)
        bs[3] = True
        assert bs[3] == True
        assert int(bs) == 8

    def test_clear_bit(self):
        bs = BitString(0xff, 8)
        bs[0] = False
        assert bs[0] == False
        assert int(bs) == 0xfe

    def test_negative_index(self):
        bs = BitString(0, 8)
        bs[-1] = True
        assert bs[7] == True


class TestSetItemSlice:
    def test_basic_slice_assign(self):
        bs = BitString(0x00, 16)
        bs[4:12] = BitString(0xff, 8)
        assert (int(bs) >> 4) & 0xff == 0xff
        # bits 0-3 and 12-15 still zero
        assert int(bs) & 0xf == 0
        assert (int(bs) >> 12) & 0xf == 0

    def test_slice_assign_preserves_surroundings(self):
        bs = BitString(0xffff, 16)
        bs[4:12] = BitString(0x00, 8)
        assert int(bs) == 0xf00f

    def test_slice_length_mismatch_raises(self):
        bs = BitString(0, 16)
        with pytest.raises(ValueError):
            bs[0:8] = BitString(0, 4)

    def test_unaligned_slice_assign(self):
        bs = BitString(0, 10)
        bs[1:6] = BitString(0x1f, 5)
        for i in range(1, 6):
            assert bs[i] == True
        assert bs[0] == False
        assert bs[6] == False


class TestBitStringSlice:
    def test_nested_slice(self):
        bs = BitString(0xdeadbeef, 32)
        s1 = bs[8:24]  # middle 16 bits
        s2 = s1[4:12]  # middle 8 of those
        assert len(s2) == 8
        assert int(s2) == (0xdeadbeef >> 12) & 0xff

    def test_single_bit_from_slice(self):
        bs = BitString(0b11001010, 8)
        s = bs[2:6]
        assert s[0] == False  # bit 2 of original
        assert s[1] == True   # bit 3 of original

    def test_slice_add(self):
        bs = BitString(0xab, 8)
        s = bs[0:4]
        result = s + BitString(0xf, 4)
        assert isinstance(result, BitString)
        assert len(result) == 8

    def test_slice_data(self):
        bs = BitString(0xabcd, 16)
        s = bs[0:8]
        assert bytes(s) == b'\xcd'

    def test_slice_equality(self):
        a = BitString(0xaa, 8)
        b = BitString(0xaa, 8)
        assert a[0:8] == b[0:8]


class TestRoundTrips:
    def test_int_roundtrip(self):
        for val in [0, 1, 0xff, 0x1234, 0xdeadbeef]:
            bs = BitString(val, 32)
            assert int(bs) == val

    def test_bytes_roundtrip(self):
        data = b'\x01\x02\x03\x04'
        bs = BitString(data)
        assert bytes(bs) == data

    def test_concat_then_slice(self):
        a = BitString(0xaa, 8)
        b = BitString(0xbb, 8)
        c = a + b
        assert int(c[0:8]) == 0xaa
        assert int(c[8:16]) == 0xbb

    def test_slice_assignment_roundtrip(self):
        bs = BitString(0, 32)
        bs[0:8] = BitString(0xaa, 8)
        bs[8:16] = BitString(0xbb, 8)
        bs[16:24] = BitString(0xcc, 8)
        bs[24:32] = BitString(0xdd, 8)
        assert int(bs) == 0xddccbbaa
