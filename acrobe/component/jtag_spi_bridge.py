from ..engine import Batcher
from ..node import Node
from ..protocol import spi
from .nsl.bnoc.fifo import JtagFifo
from .nsl.bnoc.framed import JtagFramed
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


def jtag_spi_bridge(tap, base_freq):
    """Build SPI interface stack over JTAG USER DR FIFO.

    Stack: JtagFifo → JtagFramed → SpiTransactor → spi.Interface → spi.Target
    """
    fifo = JtagFifo(tap, tap.USER_IR[0], tap.USER_IR[1])
    framed = JtagFramed(fifo)
    codec = SpiTransactor(base_freq)
    adapter = _SpiFramedAdapter(codec, framed)
    interface = spi.Interface(adapter, name="spi")
    target = spi.Target(interface, cs=0, mode=0, name="cs0")
    interface.child_add(target)
    return interface
