from .model import Segment, Program
from ..db import NoMatch
from ..endian import bitswap8


HEADER = b"\xff\x00Lattice Semiconductor Corporation Bitstream\x00"
HEADER_END = b"\x00\xff"
SYNC = bytes([0xff, 0xff, 0xbd, 0xb3, 0xff, 0xff])
SYNC_SWAPPED = bitswap8(SYNC)


@Program.ext_db.register("bin")
@Program.format_db.register("bin", "lattice")
def load_lattice_bitstream(filename, offset=0):
    with open(filename, "rb") as f:
        blob = f.read()

    start = blob.find(SYNC)
    swapped = False

    if start < 0:
        start = blob.find(SYNC_SWAPPED)
        if start < 0 or start >= 1024:
            raise NoMatch("lattice bitstream", filename)
        swapped = True

    if start >= 1024:
        raise NoMatch("lattice bitstream", filename)

    program = Program(filename)

    has_header = blob.startswith(HEADER)
    if has_header:
        header_end = blob.index(HEADER_END)
        for field in str(blob[len(HEADER):header_end], "ascii").split("\x00"):
            if ": " in field:
                k, v = field.split(": ", 1)
                program.info[k] = v
    elif blob[:2] == b"\xff\x00":
        # Yosys-style short header: ff 00 <ascii metadata> 00 ...
        nul = blob.index(b"\x00", 2)
        meta = str(blob[2:nul], "ascii", "ignore")
        for field in meta.split("\x00"):
            if ": " in field:
                k, v = field.split(": ", 1)
                program.info[k] = v

    if swapped:
        blob = bitswap8(blob)

    program.append(Segment(offset, blob[start:], filename))
    return program
