"""Host side of Altera's SLD hub, the fabric switch Quartus inserts in
front of virtual JTAG nodes.

The hub owns the TAP's USER0 and USER1 instructions.  A DR scan under
USER1 loads the hub's virtual IR (VIR): the address of a node in its
upper bits and that node's own IR in the lower ``ir_width`` bits.
Address 0 is the hub itself.  A DR scan under USER0 then reaches the
addressed node's data register, with nothing added around it.  Nodes
are not chained: one is addressed at a time.

With the hub itself addressed and its IR at zero, each 4-bit USER0 scan
captures the next nibble of an identification ROM, least significant
nibble first: one 32-bit hub word, then one 32-bit word per node.  A
node word carries a JEP106 manufacturer packed as in a JTAG IDCODE, a
type, a version and an instance number, so a node's identity is a
:class:`~acrobe.part_id.PartId` with the type as part number and the
version as revision.  Nodes are looked up by it in :attr:`SldHub.db`,
the way TAPs are in ``Tap.db``.
"""

from dataclasses import dataclass

from ...bitstring import BitString
from ...db import Db, NoMatch
from ...node import Node
from ...part_id import PartId


@dataclass(frozen=True)
class SldNodeInfo:
    """What the hub states about one node."""
    address: int
    part_id: PartId
    instance: int


class VirtualInstruction:
    """A node's data register under one of its IR values, callable like
    the instruction handle ``Tap.ir()`` returns.

    Every shift is preceded by the VIR scan selecting it.  That costs
    one short scan per call and keeps no state the hub could lose, to
    a Test-Logic-Reset or to another client selecting another node.
    """

    def __init__(self, hub: "SldHub", select: BitString):
        self.hub = hub
        self.select = select

    def __call__(self, tdi=None, read_tdo=None, **kwargs):
        self.hub.user1(self.select, read_tdo=False)
        return self.hub.user0(tdi, read_tdo=read_tdo, **kwargs)


class SldNode(Node):
    """A node behind the hub nothing registered a handler for, known
    by its identity only."""

    def __init__(self, hub: "SldHub", info: SldNodeInfo):
        super().__init__(f"node{info.address}")
        self.hub = hub
        self.info = info

    def instruction(self, node_ir: int) -> VirtualInstruction:
        return self.hub.instruction(self.info, node_ir)

    def __repr__(self):
        return (f"<SldNode @{self.info.address} {self.info.part_id}"
                f" instance {self.info.instance}>")


class SldHub(Node):
    MFG_ALTERA = 0x06e

    # Longer than any VIR, so loading it leaves only zeros in place.
    VIR_CLEAR_BITS = 64

    # Handlers take (hub, SldNodeInfo) and return the Node to attach.
    db = Db("SLD node",
            eq_func=lambda key, lookup: key.is_same_part(lookup))

    def __init__(self, tap, user0: int = 0x00c, user1: int = 0x00e,
                 name: str = "sld"):
        super().__init__(name)
        self.tap = tap
        self.user0 = tap.ir(user0)
        self.user1 = tap.ir(user1)
        self.ir_width = None
        self.address_width = None
        self.nodes: list[SldNodeInfo] = []

    @staticmethod
    def part_id_from_word(word: int) -> tuple[PartId, int]:
        """Split a node word into its identity and its instance number.

        Layout: [7:0] instance, [18:8] manufacturer, [26:19] type,
        [31:27] version."""
        mfg_id = (word >> 8) & 0x7ff
        return PartId(jep106_bank = mfg_id >> 7,
                      jep106_id = mfg_id & 0x7f,
                      part_no = (word >> 19) & 0xff,
                      revision = (word >> 27) & 0x1f), word & 0xff

    async def _word_read(self) -> int:
        word = 0
        for i in range(8):
            nibble = await self.user0(BitString(0, 4), read_tdo=True)
            word |= (int(nibble) & 0xf) << (4 * i)
        return word

    async def _hub_word_read(self) -> int | None:
        """Rewind the identification ROM and read the hub word, or None
        when no hub answers."""
        await self.user1(BitString(0, self.VIR_CLEAR_BITS), read_tdo=False)
        info = await self._word_read()
        if (info >> 8) & 0x7ff != self.MFG_ALTERA:
            return None
        return info

    async def probe(self) -> bool:
        """Whether the loaded design carries a hub."""
        return await self._hub_word_read() is not None

    async def start(self):
        info = await self._hub_word_read()
        if info is None:
            raise RuntimeError("No SLD hub answers")
        node_count = (info >> 19) & 0xff
        self.ir_width = info & 0xff
        self.address_width = node_count.bit_length()

        words = [await self._word_read() for _ in range(node_count)]
        for address, word in enumerate(words, 1):
            part_id, instance = self.part_id_from_word(word)
            node_info = SldNodeInfo(address, part_id, instance)
            self.nodes.append(node_info)
            self.logger.note("node %d: %s instance %d",
                             address, part_id, instance)
            try:
                node = await self.db.acall(part_id, self, node_info,
                                           allow_default=False)
            except NoMatch:
                node = SldNode(self, node_info)
            self.child_add(node)

    def instruction(self, info: SldNodeInfo,
                    node_ir: int) -> VirtualInstruction:
        assert self.ir_width is not None, "Hub not enumerated"
        assert 0 <= node_ir < (1 << self.ir_width)
        select = BitString((info.address << self.ir_width) | node_ir,
                           self.ir_width + self.address_width)
        return VirtualInstruction(self, select)

    def node_find(self, part_id: PartId) -> SldNodeInfo:
        matches = [n for n in self.nodes if n.part_id.is_same_part(part_id)]
        if len(matches) != 1:
            raise LookupError(f"{len(matches)} SLD nodes match {part_id}")
        return matches[0]


async def sld_attach(tap):
    """Give ``tap`` an ``sld`` child matching the design it now runs.

    Any hub found for an earlier design goes, and a new one is attached
    if the loaded design carries one.  Called when the TAP starts and
    after it is configured."""
    for child in tap.children:
        if child.name == "sld":
            await tap.child_remove(child)
    hub = SldHub(tap)
    if await hub.probe():
        tap.child_add(hub)


def applications_register(cls):
    """Give an Altera TAP class its SLD hub.

    ``sld`` enumerates the hub and exposes its nodes as children, which
    is how NSL's transports are reached on these parts."""

    @cls.application_db.register("sld")
    async def _sld(tap):
        return SldHub(tap)

    @cls.application_db.register("bnoc_framed_transport")
    async def _framed(tap):
        raise NotImplementedError(
            "No framed transport behind the SLD hub")

    return cls
