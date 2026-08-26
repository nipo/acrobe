"""Client-side RFC 2217: presents a SerialPort backed by a TelnetPipe."""

import asyncio

from ..engine import chain_future
from ..protocol.serial import (
    SerialPort, SerialConfig, Parity, StopBits, FlowControl, Signals,
)
from ..protocol.telnet import TelnetPipe
from . import codes
from .option import ComPortOption, ComPortRole


class ComPortClient(SerialPort, ComPortRole):
    """Exposes a remote serial port as a local SerialPort.

    Data reads/writes go through the TelnetPipe; config/signal calls
    translate to SB subnegotiations and wait for the server's echoed
    reply (code + 100).
    """

    def __init__(self, telnet: TelnetPipe, name: str = "rfc2217"):
        SerialPort.__init__(self, name)
        self._telnet = telnet
        self._opt = ComPortOption(self, initiator=True)
        telnet.option_add(self._opt)
        # Pending single-shot ack futures keyed by server-side code.
        self._pending: dict[int, asyncio.Future] = {}
        # Last observed signals (for edge detection on NOTIFY_MODEMSTATE)
        self._last_signals = Signals()
        # Last requested / confirmed config
        self._cfg = SerialConfig()

    async def start(self):
        await self._telnet.start()
        await self._opt.start(self._telnet)

    # ------------------------------------------------------------------
    # SerialPort data plane: lower every op onto the TelnetPipe
    # ------------------------------------------------------------------

    async def flush_ops(self, batch):
        for op, future in batch:
            chain_future(self._telnet.post(op), future)

    # ------------------------------------------------------------------
    # SerialPort control plane: send subcommand, await echo
    # ------------------------------------------------------------------

    async def _request(self, subcmd: int, payload: bytes = b"",
                       timeout: float = 5.0) -> bytes:
        reply_code = subcmd + codes.SERVER_SHIFT
        fut = asyncio.get_running_loop().create_future()
        self._pending[reply_code] = fut
        try:
            await self._opt.send(self._telnet, subcmd, payload)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(reply_code, None)

    async def config_set(self, cfg: SerialConfig) -> SerialConfig:
        baud_reply = await self._request(codes.SET_BAUDRATE,
                                         codes.encode_baudrate(cfg.baud))
        applied_baud = codes.decode_baudrate(baud_reply)
        await self._request(codes.SET_DATASIZE,
                            codes.encode_datasize(cfg.data_bits))
        await self._request(codes.SET_PARITY,
                            codes.encode_parity(cfg.parity))
        await self._request(codes.SET_STOPSIZE,
                            codes.encode_stopbits(cfg.stop_bits))
        await self._request(codes.SET_CONTROL,
                            codes.encode_flow(cfg.flow_control))
        self._cfg = cfg.with_(baud=applied_baud)
        return self._cfg

    async def config_get(self) -> SerialConfig:
        # SIGNATURE(0)-payload-0 is also "query" for SET_* codes:
        # per RFC 2217, sending a SET_BAUDRATE of 0 returns current rate.
        baud_reply = await self._request(codes.SET_BAUDRATE,
                                         codes.encode_baudrate(0))
        baud = codes.decode_baudrate(baud_reply)
        data_reply = await self._request(codes.SET_DATASIZE, bytes([0]))
        data_bits = codes.decode_datasize(data_reply)
        par_reply = await self._request(codes.SET_PARITY, bytes([0]))
        parity = codes.decode_parity(par_reply)
        stop_reply = await self._request(codes.SET_STOPSIZE, bytes([0]))
        stop = codes.decode_stopbits(stop_reply)
        flow_reply = await self._request(codes.SET_CONTROL,
                                         bytes([codes.CONTROL_REQUEST_FLOW]))
        flow = codes.decode_flow(flow_reply)
        self._cfg = SerialConfig(
            baud=baud, data_bits=data_bits, parity=parity,
            stop_bits=stop, flow_control=flow,
        )
        return self._cfg

    async def break_set(self, on: bool) -> None:
        payload = bytes([codes.CONTROL_BREAK_ON if on else codes.CONTROL_BREAK_OFF])
        await self._request(codes.SET_CONTROL, payload)

    async def dtr_set(self, on: bool) -> None:
        payload = bytes([codes.CONTROL_DTR_ON if on else codes.CONTROL_DTR_OFF])
        await self._request(codes.SET_CONTROL, payload)

    async def rts_set(self, on: bool) -> None:
        payload = bytes([codes.CONTROL_RTS_ON if on else codes.CONTROL_RTS_OFF])
        await self._request(codes.SET_CONTROL, payload)

    async def signals_get(self) -> Signals:
        # Ask for an unsolicited NOTIFY_MODEMSTATE by setting mask to 0xff
        # is a side-effect; RFC 2217 lacks a direct "poll" primitive.
        # Workaround: set mask to 0xff, which makes server push current
        # state if any bits set; we rely on last observed snapshot.
        return self._last_signals

    async def flush(self, tx: bool = True, rx: bool = True) -> None:
        what = 0
        if rx: what |= codes.PURGE_RX
        if tx: what |= codes.PURGE_TX
        if what == 0:
            return
        await self._request(codes.PURGE_DATA, bytes([what]))

    # ------------------------------------------------------------------
    # ComPortRole: handle server-originated subcommands
    # ------------------------------------------------------------------

    async def on_subcmd(self, telnet: TelnetPipe, code: int, payload: bytes):
        # Server-originated subcommands are in 100..112 (reply to client
        # request) and NOTIFY codes.
        if code == codes.NOTIFY_MODEMSTATE + codes.SERVER_SHIFT:
            new = codes.decode_modemstate(payload)
            old = self._last_signals
            self._last_signals = new
            if old != new:
                self._emit_signals(old, new)
            return
        if code == codes.NOTIFY_LINESTATE + codes.SERVER_SHIFT:
            from ..protocol.serial import LineState
            self._emit_linestate(LineState(codes.decode_linestate(payload)))
            return

        fut = self._pending.pop(code, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)
