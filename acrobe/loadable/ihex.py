from .model import Segment, Program


class IHexError(ValueError):
    pass


def _parse_ihex(filename, offset=0):
    segments = []
    current_address = None
    current_data = bytearray()
    extended_address = 0
    entry_point = None

    with open(filename, "r") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise IHexError(f"Line {line_no}: expected ':', got {line[0]!r}")

            raw = bytes.fromhex(line[1:])
            if len(raw) < 5:
                raise IHexError(f"Line {line_no}: record too short")

            byte_count = raw[0]
            address = (raw[1] << 8) | raw[2]
            record_type = raw[3]
            data = raw[4:4 + byte_count]
            checksum = raw[4 + byte_count]

            if len(data) != byte_count:
                raise IHexError(
                    f"Line {line_no}: expected {byte_count} data bytes, got {len(data)}"
                )

            computed = (~sum(raw[:4 + byte_count]) + 1) & 0xFF
            if checksum != computed:
                raise IHexError(
                    f"Line {line_no}: checksum mismatch: "
                    f"expected 0x{computed:02x}, got 0x{checksum:02x}"
                )

            if record_type == 0x00:
                # Data record
                full_address = extended_address + address + offset
                if current_address is not None and full_address == current_address + len(current_data):
                    # Contiguous: extend current segment
                    current_data.extend(data)
                else:
                    # New segment; flush previous if any
                    if current_data:
                        segments.append(Segment(current_address, current_data))
                    current_address = full_address
                    current_data = bytearray(data)

            elif record_type == 0x01:
                # EOF
                break

            elif record_type == 0x02:
                # Extended segment address
                extended_address = ((data[0] << 8) | data[1]) << 4

            elif record_type == 0x04:
                # Extended linear address
                extended_address = ((data[0] << 8) | data[1]) << 16

            elif record_type == 0x05:
                # Start linear address (entry point)
                entry_point = int.from_bytes(data, "big")

            else:
                raise IHexError(f"Line {line_no}: unsupported record type 0x{record_type:02x}")

    # Flush last segment
    if current_data:
        segments.append(Segment(current_address, current_data))

    p = Program(filename)
    for seg in segments:
        p.append(seg)
    if entry_point is not None:
        p.info["entry"] = entry_point
    return p


@Program.ext_db.register("hex", "ihex")
def load_ihex(filename, offset=0):
    return _parse_ihex(filename, offset)
