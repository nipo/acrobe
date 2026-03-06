_BITSWAP8_LUT = bytes(int(f'{i:08b}'[::-1], 2) for i in range(256))


def bitswap8(data: bytes) -> bytes:
    return data.translate(_BITSWAP8_LUT)


def swib_u16(w: int) -> int:
    return _BITSWAP8_LUT[(w >> 8) & 0xff] | (_BITSWAP8_LUT[w & 0xff] << 8)


def swib_u32(w: int) -> int:
    return swib_u16(w >> 16) | (swib_u16(w) << 16)
