def _ascii_escaped(data):
    return "".join(chr(c) if 0x20 <= c <= 0x7E else "." for c in data)


def _line_dump(address, data, line_bytes=16):
    blank_pre = address % line_bytes
    blank_post = -(address + len(data)) % line_bytes
    hex_part = (
        "   " * blank_pre
        + " ".join(f"{b:02x}" for b in data)
        + "   " * blank_post
    )
    ascii_part = " " * blank_pre + _ascii_escaped(data)
    return f"{address:#010x}: {hex_part} | {ascii_part}"


def hexdump(address, data, printer=print, line_bytes=16):
    addr_min = address - address % line_bytes
    end = address + len(data)
    addr_max = end + (-end) % line_bytes

    for addr in range(addr_min, addr_max, line_bytes):
        line_start = max(addr, address)
        line_end = addr + line_bytes
        chunk = data[line_start - address:line_end - address]
        printer(_line_dump(line_start, chunk, line_bytes=line_bytes))
