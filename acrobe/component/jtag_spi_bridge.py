from ..engine import Batcher
from ..node import Node
from ..protocol import spi
from .nsl.bnoc.framed import JtagFramed
from .nsl.transactor.spi import SpiTransactor
from .nsl.jtag_continuous_transport import ContinuousTransport


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

    Stack: ContinuousTransport → SpiTransactor → spi.Interface → spi.Target

    `base_freq` is the bridge's system clock rate, which the SPI
    transactor divides into SCK."""

    def __init__(self, tap, base_freq: float, name: str = "spi"):
        self.__codec = SpiTransactor(base_freq)
        framed = ContinuousTransport(tap, tap.USER_IR[0])
        super().__init__(_SpiFramedAdapter(self.__codec, framed), name=name)
        self.child_add(spi.Target(self, cs=0, mode=0, name="cs0"))

    def freq_update(self, freq):
        return self.__codec.freq_update(freq)
