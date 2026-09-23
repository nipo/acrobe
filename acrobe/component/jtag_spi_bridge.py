from ..engine import Batcher
from ..node import Node
from ..part_id import PartId
from ..protocol import spi
from .altera.sld_hub import SldHub, SldNodeInfo
from .nsl.bnoc.framed import JtagFramed
from .nsl.jtag_continuous_transport import ContinuousTransport
from .nsl.transactor.spi import SpiTransactor


class _SpiFramedAdapter(Batcher, Node):
    """Drives a `SpiTransactor` codec over a `JtagFramed` channel.

    Encodes the batch, sends one frame, awaits the response frame,
    and lets the codec decode it back into op futures."""

    def __init__(self, codec: SpiTransactor, channel: JtagFramed,
                 name: str = "spi-xact"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__codec = codec
        self.__channel = channel

    async def flush_ops(self, batch):
        cmd, _rsp_size, gather = self.__codec.encode(batch)
        self.__channel.send(cmd)
        rsp, _ = await self.__channel.recv()
        self.__codec.decode(batch, rsp, gather)


class JtagSpiBridge(spi.Interface):
    """SPI bus behind the NSL JTAG to SPI bridge bitstream.

    Stack: transport → SpiTransactor → spi.Interface → spi.Target

    `transport` is the datagram channel reaching the bridge's
    ``nsl_jtag.continuous_transport``.  `base_freq` is the bridge's
    system clock rate, which the SPI transactor divides into SCK.

    Behind an Altera SLD hub, the bridge's transport names itself with
    :attr:`SLD_PART_ID`, and the hub attaches the bridge under it on
    enumeration.  The TAP class then states the bridge's system clock
    rate as ``JTAG_SPI_BRIDGE_FREQ``."""

    # JEP106 bank 9, code 0x7f is acrobe's own node space, as bank 11
    # is NSL's; type 0x01 is this bridge.
    SLD_PART_ID = PartId(jep106_bank = 0x9, jep106_id = 0x7f, part_no = 0x01)

    def __init__(self, transport, base_freq: float, name: str = "spi"):
        self.__codec = SpiTransactor(base_freq)
        super().__init__(_SpiFramedAdapter(self.__codec, transport), name=name)
        self.child_add(spi.Target(self, cs=0, mode=0, name="cs0"))

    def freq_update(self, freq):
        return self.__codec.freq_update(freq)

    @staticmethod
    async def sld_find(tap) -> "JtagSpiBridge | None":
        """The bridge the SLD hub of ``tap`` found, if any."""
        hub = tap.child_lookup("sld")
        if hub is None:
            return None
        await hub.start_tree()
        bridges = hub.children_of_class(JtagSpiBridge)
        if len(bridges) > 1:
            raise LookupError(f"{len(bridges)} SPI bridges behind {hub.path}")
        return bridges[0] if bridges else None


@SldHub.db.register(JtagSpiBridge.SLD_PART_ID)
def _sld_node(hub: SldHub, info: SldNodeInfo):
    base_freq = getattr(hub.tap, "JTAG_SPI_BRIDGE_FREQ", None)
    if base_freq is None:
        raise NotImplementedError(
            f"{type(hub.tap).__name__} states no SPI bridge clock rate")
    transport = ContinuousTransport(
        hub.tap, hub.instruction(info, 0),
        name = f"continuous_transport{info.instance}")
    transport.child_add(JtagSpiBridge(transport, base_freq))
    return transport
