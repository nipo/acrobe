from acrobe.stapl.aca import decompress, _text_to_bits


def test_text_to_bits_spec_example():
    """Verify base-64 to binary conversion matches Table 3 in JESD71."""
    text = "O00008Cn63PbPMRWpGBDgj6RV60"
    bits = _text_to_bits(text)

    # Table 3 gives 27 characters → 27 * 6 = 162 bits → 21 bytes (with padding)
    # First character 'O' = decimal 24 = binary 011000
    # Packed: first 6-bit value (24 = 0b011000) goes into lower 6 bits of byte 0
    assert bits[0] & 0x3F == 24  # 'O'


def test_decompress_spec_example():
    """JESD71 Section 6.6 worked example.

    Input: "O00008Cn63PbPMRWpGBDgj6RV60"
    Output: "abcdefabcdefghijkldefabc" (24 bytes)
    """
    result = decompress("O00008Cn63PbPMRWpGBDgj6RV60")
    assert result == b"abcdefabcdefghijkldefabc"


def test_decompress_whitespace_ignored():
    """Whitespace in ACA text should be ignored."""
    result = decompress("O00008Cn63Pb PMRW\npGBDgj\t6RV60")
    assert result == b"abcdefabcdefghijkldefabc"


def test_decompress_literal_only():
    """A short payload that uses only literal blocks (no repetition)."""
    # Build ACA for 3 bytes: length=3 (LE: 03 00 00 00), then one literal block (0-bit + 3 bytes)
    # Total bits: 32 (length) + 1 (type) + 24 (data) = 57 bits → 10 six-bit values
    # We'll construct this manually.

    # Desired output: bytes 0x41, 0x42, 0x43 = "ABC"
    # Length = 3 → LE bytes: 03 00 00 00
    # Literal block: bit 0, then bytes 0x41 0x42 0x43

    # Bit stream (LSB first within each byte of the packed representation):
    # Byte 0 of length: 0x03 = 00000011
    # Byte 1 of length: 0x00 = 00000000
    # Byte 2 of length: 0x00 = 00000000
    # Byte 3 of length: 0x00 = 00000000
    # Type bit: 0
    # Byte 0 of data: 0x41 = 01000001
    # Byte 1 of data: 0x42 = 01000010
    # Byte 2 of data: 0x43 = 01000011

    # Total: 32 + 1 + 24 = 57 bits
    # Pack as 6-bit values (LSB first):
    # bits[0:6] = 000011 = 3 → '3'
    # bits[6:12] = 000000 = 0 → '0'
    # bits[12:18] = 000000 = 0 → '0'
    # bits[18:24] = 000000 = 0 → '0'
    # bits[24:30] = 000000 = 0 → '0'
    # bits[30:36] = 00 (end of length) + 0 (type) + 000 (start of 0x41) = 000000 = 0 → actually...

    # This is getting complex. Let me just construct the binary and encode it.
    import struct
    length = 3
    data = b"\x41\x42\x43"

    # Build bitstream
    bit_acc = 0
    bit_count = 0

    # 32-bit LE length
    for b in struct.pack("<I", length):
        bit_acc |= b << bit_count
        bit_count += 8

    # Literal block: type=0
    # bit_acc |= 0 << bit_count  # 0 bit
    bit_count += 1

    # 3 data bytes
    for b in data:
        bit_acc |= b << bit_count
        bit_count += 8

    # Convert to 6-bit values → base-64 characters
    val_to_char = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_@"
    text = []
    remaining = bit_count
    tmp = bit_acc
    while remaining > 0:
        text.append(val_to_char[tmp & 0x3F])
        tmp >>= 6
        remaining -= 6

    aca_text = "".join(text)
    result = decompress(aca_text)
    assert result == data
