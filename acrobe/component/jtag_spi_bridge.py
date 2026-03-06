from .nsl.bnoc.fifo import JtagFifo
from .nsl.bnoc.framed import JtagFramed
from .nsl.transactor.spi import SpiTransactor
from ..protocol import spi


def jtag_spi_bridge(tap, base_freq):
    """Build SPI interface stack over JTAG USER DR FIFO.

    Stack: JtagFifo → JtagFramed → SpiTransactor → spi.Interface → spi.Target
    """
    fifo = JtagFifo(tap, tap.USER_IR[0], tap.USER_IR[1])
    framed = JtagFramed(fifo)
    adapter = SpiTransactor(framed, base_freq)
    interface = spi.Interface(adapter, name="spi")
    target = spi.Target(interface, cs=0, mode=0, name="cs0")
    interface.child_add(target)
    return interface
