import os

from .model import Segment, Program


@Program.format_db.register("literal")
class LiteralProgram(Program):
    def __init__(self, hex_data, offset=0):
        super().__init__()
        self.append(Segment(address=offset, data=bytes.fromhex(hex_data)))


@Program.format_db.register("random")
class RandomProgram(Program):
    def __init__(self, size, offset=0):
        super().__init__()
        self.append(Segment(
            address=offset,
            data=os.urandom(int(size, 0)),
        ))


@Program.format_db.register("zero")
class ZeroProgram(Program):
    def __init__(self, size, offset=0):
        super().__init__()
        self.append(Segment(address=offset, data=b"\x00" * int(size, 0)))


@Program.format_db.register("one")
class OneProgram(Program):
    def __init__(self, size, offset=0):
        super().__init__()
        self.append(Segment(address=offset, data=b"\xff" * int(size, 0)))
