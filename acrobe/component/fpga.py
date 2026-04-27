from ..node import Node, Readable
from ..db import Db, NoMatch


class SramFpga(Node):
    """Abstract base for SRAM-based (volatile) FPGAs."""

    application_db = Db("SramFpga application")

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.application_db = Db(f"{cls.__name__} application")

    async def child_spawn(self, name):
        for cls in type(self).__mro__:
            if not (isinstance(cls, type) and issubclass(cls, SramFpga)):
                continue
            try:
                return await cls.application_db.acall(name, self)
            except NoMatch:
                continue
        raise NoMatch("child", name)

    async def load(self, source: Readable):
        """Load bitstream from a Readable Node.

        `source` is the byte-source — typically the `bitstream`
        child of a parsed format (e.g. `file.bit/bitstream`).
        Subclasses read `await source.read(0, source.size)` and
        access format metadata via `source.parent.metadata` if
        they need things like UserID for skip-reload optimisation.
        """
        raise NotImplementedError

    async def erase(self):
        raise NotImplementedError

    async def is_configured(self) -> bool:
        raise NotImplementedError


class JtagSramFpga(SramFpga):
    """JTAG-programmable SRAM FPGA. Adds USER_IR contract."""

    USER_IR: list = []


def find_bitstream(node: Node) -> Readable:
    """Pick the Readable representing a bitstream payload.

    Resolution rules:
    1. If a child literally named "bitstream" (Readable) exists,
       use it. This is what every format Node attaches.
    2. Otherwise, if `node` is itself Readable and has no Readable
       children, treat `node` as the payload (raw-bytes case).
    3. Otherwise raise ValueError.
    """
    for c in node._children:
        if c.name == "bitstream" and isinstance(c, Readable):
            return c
    if isinstance(node, Readable):
        readable_kids = [c for c in node._children if isinstance(c, Readable)]
        if not readable_kids:
            return node
    raise ValueError(
        f"{getattr(node, 'path', node.name)}: cannot locate bitstream "
        "payload — pass a Readable or a node with a `bitstream` child")
