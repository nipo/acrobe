from acrobe.util.endian import bitswap8, swib_u16, swib_u32, bitswap8_lut


class TestBitswap8:
    def test_zero(self):
        assert bitswap8(b'\x00') == b'\x00'

    def test_ff(self):
        assert bitswap8(b'\xff') == b'\xff'

    def test_single_bit(self):
        assert bitswap8(b'\x01') == b'\x80'
        assert bitswap8(b'\x80') == b'\x01'

    def test_known_value(self):
        # 0b10101010 -> 0b01010101
        assert bitswap8(b'\xaa') == b'\x55'

    def test_roundtrip(self):
        data = bytes(range(256))
        assert bitswap8(bitswap8(data)) == data

    def test_multi_byte(self):
        data = b'\x01\x80\xaa'
        result = bitswap8(data)
        assert result == b'\x80\x01\x55'

    def test_lut_self_consistent(self):
        for i in range(256):
            rev = int(f'{i:08b}'[::-1], 2)
            assert bitswap8_lut[i] == rev


class TestSwibU16:
    def test_zero(self):
        assert swib_u16(0) == 0

    def test_ffff(self):
        assert swib_u16(0xffff) == 0xffff

    def test_roundtrip(self):
        for v in [0x0001, 0x0100, 0xaa55, 0x1234, 0xdead]:
            assert swib_u16(swib_u16(v)) == v

    def test_known(self):
        # 0x0001: high_byte=0x00->rev=0x00, low_byte=0x01->rev=0x80
        # result: rev_high | (rev_low << 8) = 0x00 | 0x8000 = 0x8000
        assert swib_u16(0x0001) == 0x8000


class TestSwibU32:
    def test_zero(self):
        assert swib_u32(0) == 0

    def test_ffffffff(self):
        assert swib_u32(0xffffffff) == 0xffffffff

    def test_roundtrip(self):
        for v in [0x00000001, 0x01000000, 0xaa995566, 0xdeadbeef]:
            assert swib_u32(swib_u32(v)) == v

    def test_known(self):
        # 0x00000001: swib_u16(upper=0x0000)=0x0000, swib_u16(lower=0x0001)=0x8000
        # result: 0x0000 | (0x8000 << 16) = 0x80000000
        assert swib_u32(0x00000001) == 0x80000000
