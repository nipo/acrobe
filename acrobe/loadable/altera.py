from .model import Segment, Program
from ..db import NoMatch
from ..endian import bitswap8


SYNC = bytes([0x6a, 0xf7, 0xf7, 0xf7])
SYNC_SWAPPED = bitswap8(SYNC)


@Program.ext_db.register("rbf")
@Program.ext_db.register("bin")
@Program.format_db.register("rbf", "altera")
def load_altera_rbf(filename, offset=0):
    with open(filename, "rb") as f:
        blob = f.read()

    start = blob.find(SYNC)
    swapped = False

    if start < 0:
        start = blob.find(SYNC_SWAPPED)
        if start < 0 or start >= 1024:
            raise NoMatch("altera rbf", filename)
        swapped = True

    if start >= 1024:
        raise NoMatch("altera rbf", filename)

    program = Program(filename)
    program.info["format"] = "altera_rbf"

    if swapped:
        blob = bitswap8(blob)

    # Keep the entire file including 0xFF preamble before sync word —
    # the FPGA configuration receiver expects it.
    program.append(Segment(offset, blob, filename))
    return program
