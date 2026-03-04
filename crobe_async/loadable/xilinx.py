import struct
import gzip

from .model import Segment, Program


_HEADER = bytes([
    0x00, 0x09, 0x0f, 0xf0, 0x0f, 0xf0,
    0x0f, 0xf0, 0x0f, 0xf0, 0x00, 0x00, 0x01,
])


@Program.ext_db.register("bit")
@Program.format_db.register("bit", "xilinx")
def load_xilinx_bitstream(filename, offset=0):
    if filename.endswith(".bit.gz"):
        fd = gzip.open(filename, 'rb')
    else:
        fd = open(filename, 'rb')

    try:
        return _parse_bitstream(fd, filename, offset)
    finally:
        fd.close()


def _parse_bitstream(fd, filename, offset):
    header = fd.read(len(_HEADER))
    if header != _HEADER:
        raise ValueError(f"Bad header in {filename}")

    info = {}

    while True:
        section = fd.read(1)
        if not section:
            break

        if section == b'e':
            size, = struct.unpack(">L", fd.read(4))
            blob = fd.read(size)
            if len(blob) != size:
                raise ValueError(f"Short payload in {filename}", len(blob), size)

            program = Program(filename)
            program.append(Segment(offset, blob, filename))

            date = info[b'c'].strip() + " " + info[b'd'].strip()
            program.info["build_date"] = date
            program.info["device"] = info[b'b']
            parts = info[b'a'].split(';')
            program.info["project"] = parts[0]
            for p in parts[1:]:
                k, v = p.split('=')
                k = k.lower()
                if k == 'userid':
                    v = int(v, 16)
                program.info[k] = v

            return program

        size, = struct.unpack(">H", fd.read(2))
        blob = fd.read(size)
        if len(blob) != size:
            raise ValueError(f"Short payload in {filename}", len(blob), size)

        info[section] = str(blob.rstrip(b'\x00'), 'utf-8', 'ignore')

    raise ValueError(f"Not bitstream data in {filename}")
