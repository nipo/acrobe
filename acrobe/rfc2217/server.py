"""Server-side RFC 2217: bridges a TelnetPipe to a local SerialPort."""

import asyncio

from ..protocol.serial import (
    SerialPort, SerialConfig, Signals, LineState,
)
from ..protocol.telnet import TelnetPipe
from . import codes
from .option import ComPortOption, ComPortRole


class ComPortServer(ComPortRole):
    """Serves a SerialPort over a TelnetPipe.

    Handles client SBs by calling into the SerialPort, and relays
    serial-port events (signal edges, line-state errors) as NOTIFY_*
    subcommands back to the client.

    The data-plane bridging (port → telnet and telnet → port) runs as
    two background tasks spawned by start().
    """

    def __init__(self, telnet: TelnetPipe, serial: SerialPort):
        self._telnet = telnet
        self._serial = serial
        self._opt = ComPortOption(self, initiator=True)
        telnet.option_add(self._opt)
        self._modem_mask = 0xff  # notify everything by default
        self._line_mask = 0x00   # notify nothing unless client asks
        self._last_signals = Signals()
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        await self._telnet.start()
        await self._opt.start(self._telnet)
        # Hook serial events → notify client
        self._serial.on_signals(self._on_serial_signals)
        self._serial.on_linestate(self._on_serial_linestate)
        # Spawn data-plane bridges
        self._tasks.append(asyncio.create_task(self._pump_serial_to_telnet()))
        self._tasks.append(asyncio.create_task(self._pump_telnet_to_serial()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Data plane
    # ------------------------------------------------------------------

    async def _pump_serial_to_telnet(self):
        try:
            while True:
                # Streaming read: at least one byte, whatever arrived.
                data = await self._serial.read(None)
                await self._telnet.write(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _pump_telnet_to_serial(self):
        try:
            while True:
                data = await self._telnet.read(None)
                await self._serial.write(data)
        except asyncio.CancelledError:
            raise
        except (EOFError, Exception):
            pass

    # ------------------------------------------------------------------
    # Serial-port event hooks → NOTIFY subcommands
    # ------------------------------------------------------------------

    def _on_serial_signals(self, old: Signals, new: Signals):
        self._last_signals = new
        if self._modem_mask == 0:
            return
        asyncio.create_task(
            self._opt.send(self._telnet,
                           codes.NOTIFY_MODEMSTATE + codes.SERVER_SHIFT,
                           codes.encode_modemstate(new)))

    def _on_serial_linestate(self, flags: LineState):
        if self._line_mask == 0:
            return
        filtered = int(flags) & self._line_mask
        if filtered == 0:
            return
        asyncio.create_task(
            self._opt.send(self._telnet,
                           codes.NOTIFY_LINESTATE + codes.SERVER_SHIFT,
                           codes.encode_linestate(filtered)))

    # ------------------------------------------------------------------
    # Role: handle client subcommands
    # ------------------------------------------------------------------

    async def _reply(self, client_code: int, payload: bytes = b""):
        await self._opt.send(self._telnet,
                             client_code + codes.SERVER_SHIFT, payload)

    async def on_subcmd(self, telnet: TelnetPipe, code: int, payload: bytes):
        handler = {
            codes.SIGNATURE:           self._h_signature,
            codes.SET_BAUDRATE:        self._h_baudrate,
            codes.SET_DATASIZE:        self._h_datasize,
            codes.SET_PARITY:          self._h_parity,
            codes.SET_STOPSIZE:        self._h_stopsize,
            codes.SET_CONTROL:         self._h_control,
            codes.FLOWCONTROL_SUSPEND: self._h_flow_suspend,
            codes.FLOWCONTROL_RESUME:  self._h_flow_resume,
            codes.SET_LINESTATE_MASK:  self._h_linestate_mask,
            codes.SET_MODEMSTATE_MASK: self._h_modemstate_mask,
            codes.PURGE_DATA:          self._h_purge,
        }.get(code)
        if handler is None:
            return
        await handler(payload)

    async def _h_signature(self, payload: bytes):
        # No signature text set; echo empty.
        await self._reply(codes.SIGNATURE, b"acrobe")

    async def _h_baudrate(self, payload: bytes):
        requested = codes.decode_baudrate(payload)
        if requested == 0:
            cfg = await self._serial.config_get()
        else:
            cur = await self._serial.config_get()
            cfg = await self._serial.config_set(cur.with_(baud=requested))
        await self._reply(codes.SET_BAUDRATE, codes.encode_baudrate(cfg.baud))

    async def _h_datasize(self, payload: bytes):
        bits = codes.decode_datasize(payload)
        cur = await self._serial.config_get()
        if bits == 0:
            cfg = cur
        else:
            cfg = await self._serial.config_set(cur.with_(data_bits=bits))
        await self._reply(codes.SET_DATASIZE,
                          codes.encode_datasize(cfg.data_bits))

    async def _h_parity(self, payload: bytes):
        if payload == bytes([codes.PARITY_REQUEST]):
            cfg = await self._serial.config_get()
        else:
            parity = codes.decode_parity(payload)
            cur = await self._serial.config_get()
            cfg = await self._serial.config_set(cur.with_(parity=parity))
        await self._reply(codes.SET_PARITY, codes.encode_parity(cfg.parity))

    async def _h_stopsize(self, payload: bytes):
        if payload == bytes([codes.STOPSIZE_REQUEST]):
            cfg = await self._serial.config_get()
        else:
            stop = codes.decode_stopbits(payload)
            cur = await self._serial.config_get()
            cfg = await self._serial.config_set(cur.with_(stop_bits=stop))
        await self._reply(codes.SET_STOPSIZE,
                          codes.encode_stopbits(cfg.stop_bits))

    async def _h_control(self, payload: bytes):
        if len(payload) != 1:
            return
        v = payload[0]
        if v == codes.CONTROL_REQUEST_FLOW:
            cfg = await self._serial.config_get()
            await self._reply(codes.SET_CONTROL, codes.encode_flow(cfg.flow_control))
            return
        if v in (codes.CONTROL_FLOW_NONE, codes.CONTROL_FLOW_XONXOFF,
                 codes.CONTROL_FLOW_RTSCTS, codes.CONTROL_FLOW_DSRDTR):
            flow = codes.decode_flow(payload)
            cur = await self._serial.config_get()
            cfg = await self._serial.config_set(cur.with_(flow_control=flow))
            await self._reply(codes.SET_CONTROL,
                              codes.encode_flow(cfg.flow_control))
            return
        if v == codes.CONTROL_BREAK_ON:
            await self._serial.break_set(True)
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_BREAK_ON]))
            return
        if v == codes.CONTROL_BREAK_OFF:
            await self._serial.break_set(False)
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_BREAK_OFF]))
            return
        if v == codes.CONTROL_DTR_ON:
            await self._serial.dtr_set(True)
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_DTR_ON]))
            return
        if v == codes.CONTROL_DTR_OFF:
            await self._serial.dtr_set(False)
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_DTR_OFF]))
            return
        if v == codes.CONTROL_RTS_ON:
            await self._serial.rts_set(True)
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_RTS_ON]))
            return
        if v == codes.CONTROL_RTS_OFF:
            await self._serial.rts_set(False)
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_RTS_OFF]))
            return
        # Queries for break/DTR/RTS: we don't track break state; return OFF.
        if v == codes.CONTROL_REQUEST_BREAK:
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_BREAK_OFF]))
            return
        if v == codes.CONTROL_REQUEST_DTR:
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_DTR_ON]))
            return
        if v == codes.CONTROL_REQUEST_RTS:
            await self._reply(codes.SET_CONTROL, bytes([codes.CONTROL_RTS_ON]))
            return

    async def _h_flow_suspend(self, payload: bytes):
        # We do not implement flow suspend on the local port; ack anyway.
        await self._reply(codes.FLOWCONTROL_SUSPEND)

    async def _h_flow_resume(self, payload: bytes):
        await self._reply(codes.FLOWCONTROL_RESUME)

    async def _h_linestate_mask(self, payload: bytes):
        if len(payload) == 1:
            self._line_mask = payload[0]
        await self._reply(codes.SET_LINESTATE_MASK, bytes([self._line_mask]))

    async def _h_modemstate_mask(self, payload: bytes):
        if len(payload) == 1:
            self._modem_mask = payload[0]
        await self._reply(codes.SET_MODEMSTATE_MASK, bytes([self._modem_mask]))

    async def _h_purge(self, payload: bytes):
        if len(payload) != 1:
            return
        what = payload[0]
        tx = bool(what & codes.PURGE_TX)
        rx = bool(what & codes.PURGE_RX)
        await self._serial.flush(tx=tx, rx=rx)
        await self._reply(codes.PURGE_DATA, bytes([what]))

    async def on_ready(self, telnet: TelnetPipe):
        # On negotiation success, push an initial modem-state snapshot.
        try:
            sig = await self._serial.signals_get()
        except Exception:
            sig = Signals()
        self._last_signals = sig
        await self._opt.send(
            telnet,
            codes.NOTIFY_MODEMSTATE + codes.SERVER_SHIFT,
            codes.encode_modemstate(sig),
        )
