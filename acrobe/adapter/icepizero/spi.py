"""iCEPi-zero UART-SPI bridge host stack.

Counterpart to the ``uart_spi_bridge`` FPGA firmware. Once the ECP5 is
configured with that bitstream, the FT231X channel is reopened as a
plain 3 Mbaud UART (XON/XOFF handled by the FTDI) and carries a single
HDLC-framed, routed command stream:

    pipe (FtdiTransport UART)
      └── Hdlc              HDLC flags + byte stuffing + FCS
            └── HdlcHeader  fixed 2-byte address/control header
                  └── Router                routed multiplex
                        ├── FramedEndpoint(dst 0)  control_status  (status[0] = clock_hz)
                        └── FramedEndpoint(dst 1)  SPI transactor

The firmware routes on the destination nibble and wraps each transactor
in a ``routed_endpoint`` — the request/response framing that the host
`FramedEndpoint` (auto-incrementing tag) pairs with. The response
router forwards every destination to the host, so the host local ID is
arbitrary; ID 0 is used.
"""

from __future__ import annotations

from ...engine import Batcher
from ...node import Node
from ...protocol import spi
from ...component.hdlc import Hdlc, HdlcHeader
from ...component.nsl.bnoc.routed import Router, FramedEndpoint
from ...component.nsl.transactor.spi import SpiTransactor
from ...component.nsl.transactor.control_status import (
    ControlStatusTransactor, RegRead, RegWrite,
)


# Routing endpoint IDs match the firmware topcell routing table. The
# response router forwards every destination to the host, so the host
# local ID is free — 0 fits.
LOCAL_ID = 0
CS_DST = 0
SPI_DST = 1

# control_status status register holding the SPI transactor base clock.
CLOCK_HZ_REG = 0


def _decode_on_recv(endpoint, codec, batch, cmd, gather):
    """Non-blocking transactor flush: post the command and one recv on
    ``endpoint``, then decode the response into the batch's futures from
    the recv callback. Never awaits — see the Batcher flush_ops
    contract."""
    endpoint.send(cmd)
    rf = endpoint.recv()

    def done(f):
        exc = f.exception()
        if exc is None:
            try:
                rsp, _ctx = f.result()
                codec.decode(batch, rsp, gather)
                return
            except BaseException as e:   # noqa: BLE001 — forward to futures
                exc = e
        for _op, fut in batch:
            if fut is not None and not fut.done():
                fut.set_exception(exc)

    rf.add_done_callback(done)


class _ControlStatus(Batcher, Node):
    """Drives a `ControlStatusTransactor` over a `FramedEndpoint`."""

    def __init__(self, endpoint: FramedEndpoint, name: str = "cs"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__endpoint = endpoint
        self.__codec = ControlStatusTransactor()

    def reg_read(self, reg: int):
        return self.post(RegRead(int(reg)))

    def reg_write(self, reg: int, value: int):
        return self.post(RegWrite(int(reg), int(value)))

    async def flush_ops(self, batch):
        cmd, _rsp_size, gather = self.__codec.encode(batch)
        _decode_on_recv(self.__endpoint, self.__codec, batch, cmd, gather)


class _UartSpiAdapter(Batcher, Node):
    """Lower adapter posted to by `spi.Interface.flush_ops`.

    Encodes the batch of Cs/Shift ops via a `SpiTransactor`, drives the
    SPI `FramedEndpoint`, and resolves futures from the response. The
    codec is wired by the owning interface in `start()` once the base
    frequency has been read from control_status."""

    def __init__(self, endpoint: FramedEndpoint, name: str = "spi-xact"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__endpoint = endpoint
        self.codec: SpiTransactor | None = None

    async def flush_ops(self, batch):
        if self.codec is None:
            raise RuntimeError("_UartSpiAdapter codec not configured")
        cmd, _rsp_size, gather = self.codec.encode(batch)
        _decode_on_recv(self.__endpoint, self.codec, batch, cmd, gather)


class IcepizeroUartSpi(spi.Interface):
    """SPI bus over the iCEPi-zero UART-SPI bridge firmware.

    Owns the full HDLC->router stack and a single `spi.Target` on
    chip-select 0. Built over an already-opened UART `Pipe`; the
    underlying transport (a shared, mutually-exclusive channel) is
    owned and torn down by the `IcePiZero` board node, not here."""

    def __init__(self, pipe, name: str = "spi"):
        self.__pipe = pipe
        self.__hdlc = Hdlc(pipe)
        self.__header = HdlcHeader(self.__hdlc)
        self.__router = Router(self.__header)
        self.__cs_endpoint = FramedEndpoint(
            self.__router.route(LOCAL_ID, CS_DST), name="cs-endpoint")
        self.__spi_endpoint = FramedEndpoint(
            self.__router.route(LOCAL_ID, SPI_DST), name="spi-endpoint")
        self.__cs = _ControlStatus(self.__cs_endpoint)
        self.__adapter = _UartSpiAdapter(self.__spi_endpoint)

        super().__init__(self.__adapter, name=name)

        self.child_add(self.__pipe)
        self.__pipe.child_add(self.__hdlc)
        self.__hdlc.child_add(self.__header)
        self.__header.child_add(self.__router)
        self.__router.child_add(self.__cs_endpoint)
        self.__router.child_add(self.__spi_endpoint)
        self.__cs_endpoint.child_add(self.__cs)
        self.__spi_endpoint.child_add(self.__adapter)
        self.child_add(spi.Target(self, cs=0, mode=0, name="cs0"))

    async def start(self):
        await self.__pipe.start_tree()
        base_freq = await self.__cs.reg_read(CLOCK_HZ_REG)
        self.logger.note("NSL SPI transactor base freq %d Hz", base_freq)
        self.__adapter.codec = SpiTransactor(base_freq)
        self.freq_reapply()
        await super().start()

    def freq_update(self, freq):
        if self.__adapter.codec is None:
            return 0.0
        return self.__adapter.codec.freq_update(freq)
