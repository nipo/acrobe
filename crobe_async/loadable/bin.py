from .model import Segment, Program


@Program.ext_db.register("bin")
def load_bin(filename, offset=0):
    with open(filename, "rb") as f:
        data = f.read()
    p = Program(filename)
    p.append(Segment(offset, data))
    return p
