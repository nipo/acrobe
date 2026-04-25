import struct
import enum

from .model import Segment, Program
from ..db import NoMatch
from ..endian import bitswap8


SYNC = bytes([0x6a, 0xf7, 0xf7, 0xf7])
SYNC_SWAPPED = bitswap8(SYNC)

SOF_MAGIC = b"SOF\x00"


# --- RBF loader ---

@Program.ext_db.register("rbf")
@Program.format_db.register("altera_rbf")
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

class SofSection(enum.IntEnum):
    """SOF section tags"""
    TOOL = 0x01
    DEVICE = 0x02
    DESIGN = 0x03
    END = 0x08
    CONFIG_DATA = 0x11
    CONFIG_INFO = 0x12
    METADATA = 0x13
    CHECKSUM = 0x15
    EMBEDDED = 0x24

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

    version, section_count = struct.unpack_from("<II", sof, 4)

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

        if tag == SofSection.TOOL:
            result["tool"] = data.rstrip(b"\x00").decode("ascii", errors="replace")
        elif tag == SofSection.DEVICE:
            result["device"] = data.rstrip(b"\x00").decode("ascii", errors="replace")
        elif tag == SofSection.DESIGN:
            result["design"] = data.rstrip(b"\x00").decode("ascii", errors="replace")
        elif tag == SofSection.CONFIG_INFO:
            result["config_info"] = data
        elif tag == SofSection.CONFIG_DATA:
            result["config_data"] = data
        elif tag == SofSection.METADATA:
            # 16-byte section: 4 unknown + 4 unknown + 4 unknown + 4 usercode
            if length >= 16:
                result["usercode"] = struct.unpack_from("<I", data, 12)[0]
        elif tag == SofSection.CHECKSUM:
            result["checksum"] = data
        elif tag == SofSection.END:
            break

    return result


POF_MAGIC = b"POF\x00"

# POF section tags (same numbering as SOF)
_POF_TOOL = 0x01
_POF_FLASH = 0x02
_POF_DESIGN = 0x03
_POF_END = 0x08
_POF_CONFIG_DATA = 0x11
_POF_BOOT_INFO = 0x1a


def _parse_pof_sections(pof):
    """Parse POF/SOF tag-length-value sections."""
    idx = 12  # skip magic(4) + version(4) + count(4)
    sections = []
    while idx + 6 <= len(pof):
        tag = pof[idx]
        flags = pof[idx + 1]
        length = struct.unpack_from("<I", pof, idx + 2)[0]
        data = pof[idx + 6:idx + 6 + length]
        sections.append((tag, flags, data))
        idx += 6 + length
        if tag == _POF_END:
            break
    return sections


def _parse_boot_info(data):
    """Parse BOOT_INFO string into partition entries.

    Format: "NAME ADDR SIZE;NAME ADDR SIZE;..."
    Returns list of (name, address, size).
    """
    text = data.rstrip(b'\x00').decode('ascii', errors='replace')
    partitions = []
    for entry in text.split(';'):
        tokens = entry.split()
        if len(tokens) >= 3:
            try:
                name = tokens[0]
                addr = int(tokens[1], 16)
                size = int(tokens[2], 16)
                partitions.append((name, addr, size))
            except ValueError:
                continue
    return partitions


_FF_GAP_THRESHOLD = 4096  # consecutive 0xFF bytes = end of RBF section


def _find_rbf_end(data):
    """Find where RBF data ends in a flash region.

    RBF data is sparse (many zero bytes) but doesn't have
    sustained runs of 0xFF longer than a few bytes. A run of
    0xFF >= _FF_GAP_THRESHOLD indicates erased flash padding
    after the RBF data.
    """
    ff_run = 0
    for i in range(len(data)):
        if data[i] == 0xFF:
            ff_run += 1
            if ff_run >= _FF_GAP_THRESHOLD:
                return i - _FF_GAP_THRESHOLD + 1
        else:
            ff_run = 0
    return len(data)


def _extract_pof_bitstream(config_data, partitions):
    """Extract RBF bitstream from POF flash image.

    The flash image contains bitswapped RBF data split across
    partitions listed in BOOT_INFO. Each partition's RBF data
    starts at offset +12 (first partition has a header, others
    have 0xFF padding). Data runs until a sustained 0xFF gap.

    The extracted chunks are concatenated and bitswapped to
    produce the equivalent of an RBF file.
    """
    parts = sorted(partitions, key=lambda p: p[1])

    chunks = []
    for i, (name, addr, size) in enumerate(parts):
        if i + 1 < len(parts):
            region_end = min(addr + size, parts[i + 1][1])
        else:
            region_end = min(addr + size, len(config_data))

        # RBF data starts at +12 in each partition
        data_start = addr + 12
        if data_start >= region_end:
            continue

        region = config_data[data_start:region_end]
        rbf_end = _find_rbf_end(region)
        chunk = region[:rbf_end]

        if chunk:
            chunks.append(chunk)

    if not chunks:
        return None

    return bitswap8(b''.join(chunks))


@Program.ext_db.register("pof")
@Program.format_db.register("pof")
def load_altera_pof(filename, offset=0):
    """Load an Altera POF file.

    Extracts the RBF bitstream from the flash image, bitswaps it
    to JTAG bit order, and returns it as a loadable program.
    Equivalent to loading the corresponding RBF file.
    """
    with open(filename, "rb") as f:
        pof = f.read()

    if pof[:4] != POF_MAGIC:
        raise NoMatch("altera pof", filename)

    sections = _parse_pof_sections(pof)

    config_data = None
    boot_info = None
    tool = None
    device = None
    design = None

    for tag, flags, data in sections:
        if tag == _POF_CONFIG_DATA:
            config_data = data
        elif tag == _POF_BOOT_INFO:
            boot_info = _parse_boot_info(data)
        elif tag == _POF_TOOL:
            tool = data.rstrip(b'\x00').decode('ascii', errors='replace')
        elif tag == _POF_FLASH:
            device = data.rstrip(b'\x00').decode('ascii', errors='replace')
        elif tag == _POF_DESIGN:
            design = data.rstrip(b'\x00').decode('ascii', errors='replace')

    if config_data is None:
        raise ValueError(f"POF file {filename} has no CONFIG_DATA section")

    if boot_info is None:
        # No partition table — treat entire config_data as bitstream
        # Skip 12-byte header, bitswap
        blob = bitswap8(config_data[12:])
    else:
        blob = _extract_pof_bitstream(config_data, boot_info)
        if blob is None:
            raise ValueError(f"POF file {filename}: no data in partitions")

    program = Program(filename)
    program.info["format"] = "altera_pof"
    if tool:
        program.info["tool"] = tool
    if device:
        program.info["flash"] = device
    if design:
        program.info["design"] = design

    program.append(Segment(offset, blob, filename))
    return program


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
