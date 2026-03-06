import gzip
from collections import deque

from .model import Segment, Program
from ..bitstring import BitString


@Program.ext_db.register("fs.gz")
@Program.ext_db.register("fs")
@Program.format_db.register("fs")
def load_gowin_bitstream(filename, offset=0):
    if filename.endswith(".fs.gz"):
        fd = gzip.open(filename, 'rt', encoding='utf-8', errors='ignore')
    else:
        fd = open(filename, 'r')

    try:
        return _parse_fs(fd, filename, offset)
    finally:
        fd.close()


def _parse_fs(fd, filename, offset):
    lines = deque(fd.readlines())

    info = {}
    while lines and lines[0].startswith("//"):
        line = lines.popleft().strip()[2:]
        if ':' in line:
            k, v = line.split(":", 1)
            info[k] = v.strip()

    stream = "".join(l.strip() for l in lines)
    if not stream:
        raise ValueError(f"No bitstream data in {filename}")

    data = BitString(int(stream, 2), len(stream))
    raw = bytes(data)[::-1]

    program = Program(filename)
    program.info = info
    program.append(Segment(offset, raw, filename))
    return program
