import struct

from .model import Segment, Program
from ..db import NoMatch
from ..endian import bitswap8


SYNC = bytes([0x6a, 0xf7, 0xf7, 0xf7])
SYNC_SWAPPED = bitswap8(SYNC)

SOF_MAGIC = b"SOF\x00"


# --- RBF loader ---

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


# --- SOF parser ---

# SOF section tags
_SOF_TOOL = 0x01
_SOF_DEVICE = 0x02
_SOF_DESIGN = 0x03
_SOF_END = 0x08
_SOF_CONFIG_DATA = 0x11
_SOF_CONFIG_INFO = 0x12
_SOF_METADATA = 0x13
_SOF_CHECKSUM = 0x15
_SOF_EMBEDDED = 0x24


def parse_sof(filename):
    """Parse an Altera/Intel SOF file.

    Handles both raw SOF and gzip-compressed SOF (Agilex).

    Returns a dict with:
        "tool": str — Quartus version
        "device": str — target device (e.g. "10CL025YU256C8G")
        "design": str — design name
        "usercode": int or None
        "config_info": bytes — raw CONFIG_INFO section
        "config_data": bytes — raw CONFIG_DATA section (frame-based, not RBF)
        "checksum": bytes or None
        "sections": list of (tag, flags, data) for all sections
    """
    with open(filename, "rb") as f:
        sof = f.read()

    # Auto-detect gzip wrapper
    if sof[:2] == b'\x1f\x8b':
        import gzip
        sof = gzip.decompress(sof)

    if sof[:4] != SOF_MAGIC:
        raise NoMatch("altera sof", filename)

    version = struct.unpack_from("<I", sof, 4)[0]
    section_count = struct.unpack_from("<I", sof, 8)[0]

    result = {
        "version": version,
        "section_count": section_count,
        "tool": None,
        "device": None,
        "design": None,
        "usercode": None,
        "config_info": None,
        "config_data": None,
        "checksum": None,
        "sections": [],
    }

    idx = 12
    for _ in range(section_count + 1):
        if idx + 6 > len(sof):
            break

        tag = sof[idx]
        flags = sof[idx + 1]
        length = struct.unpack_from("<I", sof, idx + 2)[0]
        data = sof[idx + 6:idx + 6 + length]
        idx += 6 + length

        result["sections"].append((tag, flags, data))

        if tag == _SOF_TOOL:
            result["tool"] = data.rstrip(b"\x00").decode("ascii", errors="replace")
        elif tag == _SOF_DEVICE:
            result["device"] = data.rstrip(b"\x00").decode("ascii", errors="replace")
        elif tag == _SOF_DESIGN:
            result["design"] = data.rstrip(b"\x00").decode("ascii", errors="replace")
        elif tag == _SOF_CONFIG_INFO:
            result["config_info"] = data
        elif tag == _SOF_CONFIG_DATA:
            result["config_data"] = data
        elif tag == _SOF_METADATA:
            # 16-byte section: 4 unknown + 4 unknown + 4 unknown + 4 usercode
            if length >= 16:
                result["usercode"] = struct.unpack_from("<I", data, 12)[0]
        elif tag == _SOF_CHECKSUM:
            result["checksum"] = data
        elif tag == _SOF_END:
            break

    return result


@Program.ext_db.register("sof")
@Program.format_db.register("sof")
def load_altera_sof(filename, offset=0):
    """Load an Altera SOF file.

    Extracts metadata. The config data in SOF is in Quartus internal
    frame format, NOT RBF bitstream format — SOF→RBF conversion is
    not yet implemented.  For now, the raw config data is stored as
    the program segment.
    """
    parsed = parse_sof(filename)

    if parsed["config_data"] is None:
        raise ValueError(f"SOF file {filename} has no CONFIG_DATA section")

    program = Program(filename)
    program.info["format"] = "altera_sof"

    if parsed["device"]:
        program.info["device"] = parsed["device"]
    if parsed["design"]:
        program.info["design"] = parsed["design"]
    if parsed["tool"]:
        program.info["tool"] = parsed["tool"]
    if parsed["usercode"] is not None:
        program.info["usercode"] = parsed["usercode"]

    program.append(Segment(offset, parsed["config_data"], filename))
    return program
