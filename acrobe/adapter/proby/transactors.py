"""Proby ``jtag_swd_i2c`` firmware host stack.

When the Proby's channel-A FPGA is loaded with the ``jtag_swd_i2c``
bitstream, channel A becomes a FT245 synchronous FIFO carrying a
single chunk-framed wire stream multiplexed by a router into per-
protocol endpoints. The host-side counterpart of that firmware is
the protocol stack assembled here:

    pipe (Ft245SyncPipe)
      └── Chunked          length-prefixed chunk framing
            └── Router      routed multiplex
                  ├── FramedEndpoint(route 0xf↔0x3)  control_status
                  └── FramedEndpoint(route 0xf↔dst)  per-protocol

The four concrete interfaces (`ProbySwdInterface`,
`ProbyJtagInterface`, `ProbyI2cInterface`, `ProbySpiInterface`) each
own one instance of this stack and the matching transactor codec.
They're spawned by :class:`acrobe.adapter.proby.ProbyAdapter` on
demand and torn down when the adapter is asked for a different
protocol.
"""

from __future__ import annotations

from ...engine import Batcher
from ...node import Node
from ...protocol import i2c, jtag, spi, swd
from ...component.nsl.bnoc.chunked import Chunked
from ...component.nsl.bnoc.routed import Router, FramedEndpoint
from ...component.nsl.transactor.swd import SwdTransactor
from ...component.nsl.transactor.jtag import JtagTransactor
from ...component.nsl.transactor.i2c import I2cTransactor
from ...component.nsl.transactor.spi import SpiTransactor
from ...component.nsl.transactor.control_status import (
    ControlStatusTransactor, RegRead, RegWrite,
)


# Routing endpoint IDs match the firmware topcell.
LOCAL_ID = 0xf
SWD_DST   = 0x0
JTAG_DST  = 0x1
I2C_DST   = 0x2
CS_DST    = 0x3
CC_DST    = 0x4
SPI_DST   = 0x5

# Control-status mode register: written to CS register 3 at bring-up.
MODE_REG  = 3
BASE_FREQ_REG = 0
MODE_SWD     = 0
MODE_JTAG    = 1
MODE_SPI     = 3
MODE_SPI_INV = 4


def _decode_on_recv(endpoint, codec, batch, cmd, gather):
    """Non-blocking transactor flush: post the command and one recv,
    then decode the response into the batch's futures from the recv
    callback. Never awaits — see the Batcher flush_ops contract."""
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


class ProbyControlStatus(Batcher, Node):
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


class _ProbyStack:
    """Shared bookkeeping for the chunked→router→cs scaffold under a
    Proby channel-A protocol interface.

    Not a Node — the owning Interface attaches each part as one of
    its visible children."""

    def __init__(self, pipe):
        self.pipe = pipe
        self.chunked = Chunked(pipe)
        self.router = Router(self.chunked)
        self.cs_endpoint = FramedEndpoint(
            self.router.route(LOCAL_ID, CS_DST), name="cs-endpoint")
        self.cs = ProbyControlStatus(self.cs_endpoint)

    def attach_to(self, parent: Node):
        parent.child_add(self.chunked)
        self.chunked.child_add(self.router)
        self.router.child_add(self.cs_endpoint)
        self.cs_endpoint.child_add(self.cs)

    async def bring_up(self):
        # Chunked's start() sprays RESET markers to clear any dangling
        # frame on the device side — must run before any real traffic.
        await self.chunked.start_tree()


class ProbySwdInterface(swd.Interface):
    """SWD via the NSL SWD transactor over Proby's ``jtag_swd_i2c``
    firmware. Owns the full chunked→router→cs→endpoint stack."""

    def __init__(self, pipe, name: str = "swd"):
        super().__init__(name)
        self.__stack = _ProbyStack(pipe)
        self.__stack.attach_to(self)
        self.__endpoint = FramedEndpoint(
            self.__stack.router.route(LOCAL_ID, SWD_DST),
            name="swd-endpoint")
        self.__stack.router.child_add(self.__endpoint)
        self.__codec: SwdTransactor | None = None

    async def start(self):
        await self.__stack.bring_up()
        base_freq = await self.__stack.cs.reg_read(BASE_FREQ_REG)
        self.logger.note(
            "NSL SWD transactor base freq %d Hz", base_freq)
        await self.__stack.cs.reg_write(MODE_REG, MODE_SWD)
        self.__codec = SwdTransactor(base_freq, divisor_width=2)
        self.freq_reapply()
        await super().start()

    async def flush_wire_ops(self, batch):
        if self.__codec is None:
            raise RuntimeError("ProbySwdInterface not started")
        cmd, _rsp_size, gather = self.__codec.encode(batch)
        _decode_on_recv(self.__endpoint, self.__codec, batch, cmd, gather)

    def freq_update(self, freq):
        if self.__codec is None:
            return 0.0
        return self.__codec.freq_update(freq)


class ProbyJtagInterface(jtag.JtagInterface):
    """JTAG via the NSL JTAG transactor over Proby's ``jtag_swd_i2c``.

    The firmware's ``CMD_SWD_TO_JTAG`` handler drives only 14 of the
    16 magic SWD-to-JTAG bits, so a switch sandwiched by just the
    default 50-cycle TLR resets is not seen reliably by the target.
    We compensate by surrounding each ``jtag.SwdToJtag`` op with our
    own line-reset cycles (50 leading TMS=1, 53 trailing TMS=1) at
    flush time, which puts the full 0xE73C sequence inside the
    spec-mandated TMS-high envelope regardless of the firmware
    truncation."""

    SWD_TO_JTAG_PREAMBLE_CYCLES = 50
    SWD_TO_JTAG_TRAILER_CYCLES = 53

    def __init__(self, pipe, name: str = "jtag"):
        super().__init__(name)
        self.__stack = _ProbyStack(pipe)
        self.__stack.attach_to(self)
        self.__endpoint = FramedEndpoint(
            self.__stack.router.route(LOCAL_ID, JTAG_DST),
            name="jtag-endpoint")
        self.__stack.router.child_add(self.__endpoint)
        self.__codec: JtagTransactor | None = None

    def optiton_set(self, key, value):
        self.__codec.optiton_set(key, value)
        
    async def start(self):
        await self.__stack.bring_up()
        base_freq = await self.__stack.cs.reg_read(BASE_FREQ_REG)
        self.logger.note(
            "NSL JTAG transactor base freq %d Hz", base_freq)
        await self.__stack.cs.reg_write(MODE_REG, MODE_JTAG)
        self.__codec = JtagTransactor(base_freq)
        self.freq_reapply()
        await super().start()

    def __expand_batch(self, batch):
        out: list = []
        for op, future in batch:
            if isinstance(op, jtag.SwdToJtag):
                out.append(
                    (jtag.Reset(count=self.SWD_TO_JTAG_PREAMBLE_CYCLES), None))
                out.append((op, future))
                out.append(
                    (jtag.Reset(count=self.SWD_TO_JTAG_TRAILER_CYCLES), None))
                continue
            out.append((op, future))
        return out

    async def flush_ops(self, batch):
        if self.__codec is None:
            raise RuntimeError("ProbyJtagInterface not started")
        expanded = self.__expand_batch(batch)
        if not expanded:
            return
        cmd, _rsp_size, gather = self.__codec.encode(expanded)
        _decode_on_recv(self.__endpoint, self.__codec, expanded, cmd, gather)

    def freq_update(self, freq):
        if self.__codec is None:
            return 0.0
        return self.__codec.freq_update(freq)


class _ProbyI2cAdapter(Batcher, Node):
    """Lower adapter posted to by `i2c.Interface.flush_ops`.

    Receives :class:`acrobe.protocol.i2c.Transaction` ops (the
    Interface normalises naked Transfer/WaitAck into Transactions
    first), encodes via :class:`I2cTransactor`, drives the
    `FramedEndpoint`, and resolves futures."""

    def __init__(self, endpoint: FramedEndpoint, name: str = "i2c-xact"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__endpoint = endpoint
        # codec is wired by the owning Interface in start(), once
        # base_freq is known.
        self.codec: I2cTransactor | None = None

    async def flush_ops(self, batch):
        if self.codec is None:
            raise RuntimeError("_ProbyI2cAdapter codec not configured")
        cmd, _rsp_size, gather = self.codec.encode(batch)
        _decode_on_recv(self.__endpoint, self.codec, batch, cmd, gather)


class ProbyI2cInterface(i2c.Interface):
    """I²C via the NSL I²C transactor over Proby's ``jtag_swd_i2c``.

    Unlike SWD/JTAG, the I²C path doesn't need a mode_set on CS
    register 3 — the firmware exposes I²C through a fixed routing
    endpoint regardless of the SWD/JTAG mux setting."""

    def __init__(self, pipe, name: str = "i2c"):
        self.__stack = _ProbyStack(pipe)
        self.__endpoint = FramedEndpoint(
            self.__stack.router.route(LOCAL_ID, I2C_DST),
            name="i2c-endpoint")
        self.__adapter = _ProbyI2cAdapter(self.__endpoint)
        super().__init__(self.__adapter, name=name)
        self.__stack.attach_to(self)
        self.__stack.router.child_add(self.__endpoint)
        self.__endpoint.child_add(self.__adapter)

    async def start(self):
        await self.__stack.bring_up()
        base_freq = await self.__stack.cs.reg_read(BASE_FREQ_REG)
        self.logger.note(
            "NSL I2C transactor base freq %d Hz", base_freq)
        self.__adapter.codec = I2cTransactor(base_freq)
        self.freq_reapply()

    def freq_update(self, freq):
        if self.__adapter.codec is None:
            return 0.0
        return self.__adapter.codec.freq_update(freq)


class _ProbySpiAdapter(Batcher, Node):
    """Lower adapter posted to by `spi.Interface.flush_ops`.

    Receives :class:`acrobe.protocol.spi.Cs` / `Shift` ops, encodes
    them via :class:`SpiTransactor`, drives the `FramedEndpoint`, and
    resolves futures from the response."""

    def __init__(self, endpoint: FramedEndpoint, name: str = "spi-xact"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self.__endpoint = endpoint
        # codec is wired by the owning Interface in start(), once
        # base_freq is known.
        self.codec: SpiTransactor | None = None

    async def flush_ops(self, batch):
        if self.codec is None:
            raise RuntimeError("_ProbySpiAdapter codec not configured")
        cmd, _rsp_size, gather = self.codec.encode(batch)
        _decode_on_recv(self.__endpoint, self.codec, batch, cmd, gather)


class ProbySpiInterface(spi.Interface):
    """SPI via the NSL SPI transactor over Proby's ``jtag_swd_i2c``.

    Like SWD/JTAG (and unlike I²C), SPI shares the debug connector's
    pins and needs a mode write on CS register 3 at bring-up. The
    firmware offers two pin mappings, selected by the ``pinout``
    option:

    * ``pinout=normal`` (default) — SCK on TCK, CS on TMS, MOSI on
      TDI, MISO on TDO.
    * ``pinout=inverted`` — same, with MOSI on TDO and MISO on TDI.

    The firmware instantiates the SPI transactor with a single slave,
    so the only usable chip select is 0; one `spi.Target` child sits
    there."""

    def __init__(self, pipe, name: str = "spi"):
        self.__mode = MODE_SPI
        self.__stack = _ProbyStack(pipe)
        self.__endpoint = FramedEndpoint(
            self.__stack.router.route(LOCAL_ID, SPI_DST),
            name="spi-endpoint")
        self.__adapter = _ProbySpiAdapter(self.__endpoint)
        super().__init__(self.__adapter, name=name)
        self.__stack.attach_to(self)
        self.__stack.router.child_add(self.__endpoint)
        self.__endpoint.child_add(self.__adapter)
        self.child_add(spi.Target(self, cs=0, mode=0, name="cs0"))

    def option_set(self, key, value):
        if key != "pinout":
            return
        modes = {"normal": MODE_SPI, "inverted": MODE_SPI_INV}
        if value not in modes:
            raise ValueError(
                f"pinout must be one of {', '.join(sorted(modes))}, "
                f"got {value!r}")
        self.__mode = modes[value]

    async def start(self):
        await self.__stack.bring_up()
        base_freq = await self.__stack.cs.reg_read(BASE_FREQ_REG)
        self.logger.note(
            "NSL SPI transactor base freq %d Hz", base_freq)
        await self.__stack.cs.reg_write(MODE_REG, self.__mode)
        self.__adapter.codec = SpiTransactor(base_freq)
        self.freq_reapply()
        await super().start()

    def freq_update(self, freq):
        if self.__adapter.codec is None:
            return 0.0
        return self.__adapter.codec.freq_update(freq)
