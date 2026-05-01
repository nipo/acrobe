"""Per-connection JoP session driver.

A :class:`JopSession` owns one set of five client sockets (CTRL, MGMT,
MGMT_RSP, H2T, T2H) and bridges them to a local
:class:`acrobe.protocol.jtag.JtagInterface`.

Data plane
----------
1. Read framed packets off the H2T socket.
2. Decode the JoP byte stream (CMD_CONFIG / CMD_WRITE_TDO_ENABLE_FIFO /
   CMD_*_TMS_TDI / CMD_LONG_FIXED_TMS_TDI) via :class:`JopDecoder`.
3. For each :class:`Shift` op, drive the JTAG interface through the
   :class:`JtagTmsWalker` and capture TDO bits where the active TDO-enable
   FIFO entry has ``tdo_enable`` set.
4. Pack captured bits LSB-first into bytes via :class:`JopEncoder` and
   send them as one or more T2H frames.

Control plane
-------------
NUL-terminated ASCII commands (PING, GET_PARAM, SET_PARAM, DISCONNECT,
GET_DRIVER_PARAM, SET_DRIVER_PARAM). Default ``MGMT_SUPPORT=0`` so the
MGMT/MGMT_RSP sockets stay quiet — Quartus skips that path when the
welcome message advertises no MGMT support.
"""

import asyncio
import logging
from collections import deque

from ..bitstring import BitString
from ..protocol import jtag
from ..protocol.jtag_walker import JtagTmsWalker

from . import bytestream as bs
from . import control
from . import framing


_logger = logging.getLogger("jop.session")


class JopSession:
    """One client connection. Constructed by :class:`JopListener`
    after the 5-socket handshake completes."""

    def __init__(self, *,
                 interface: jtag.JtagInterface,
                 ctrl: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                 mgmt: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                 mgmt_rsp: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                 h2t: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                 t2h: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                 mgmt_support: bool = False) -> None:
        self._interface = interface
        self._ctrl = ctrl
        self._mgmt = mgmt
        self._mgmt_rsp = mgmt_rsp
        self._h2t = h2t
        self._t2h = t2h
        self._mgmt_support = mgmt_support

        self._walker = JtagTmsWalker(interface, warn_bundled_entry=True)
        self._decoder = bs.JopDecoder()
        self._encoder = bs.JopEncoder()
        # Pending TDO-capture descriptors (FIFO depth 2 on-chip).
        self._capture_fifo: deque[bs.PushTdoCapture] = deque()
        # Bits remaining in the head capture window.
        self._head_remaining: int = 0
        # If True, head window's eop_gen has fired; emit EOP on next byte.
        self._eop_pending: bool = False

        # Loopback knobs. SERVER_LOOPBACK is the etherlink-level switch
        # (Intel's reference echoes H2T headers + payload back as T2H);
        # #HW_LOOPBACK is the driver-level switch that on real hardware
        # would enable the streaming-debug-IP's internal loopback CSR.
        # We don't have an IP — we just collapse both into a single
        # "echo H2T to T2H without decoding" mode, which is enough to
        # let Intel's remote_debug_tester_app exercise our wire layer.
        self._server_loopback: bool = False
        self._hw_loopback: bool = False
        # Driver-param store. Currently only #HW_LOOPBACK is recognised;
        # other names are accepted into the dict but not interpreted.
        self._driver_params: dict[str, str] = {"#HW_LOOPBACK": "0"}

        self._stop_event = asyncio.Event()

    @property
    def _loopback_active(self) -> bool:
        return self._server_loopback or self._hw_loopback

    async def serve(self) -> None:
        """Run all per-connection tasks until disconnect."""
        ctrl_task = asyncio.create_task(self._ctrl_loop())
        h2t_task = asyncio.create_task(self._h2t_loop())
        # MGMT is normally idle (the on-chip jop_blaster IP terminates
        # those FIFOs to reset). We still drain it so any unexpected
        # traffic from a tool that does use MGMT shows up in the logs
        # rather than silently piling up in the kernel buffer.
        mgmt_task = asyncio.create_task(self._mgmt_drain_loop())
        tasks = (ctrl_task, h2t_task, mgmt_task)
        try:
            _done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        finally:
            self._stop_event.set()
            for t in tasks:
                if not t.done():
                    t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    # --- Control plane ---

    async def _ctrl_loop(self) -> None:
        reader, writer = self._ctrl
        try:
            while not self._stop_event.is_set():
                line = await self._read_until_nul(reader)
                if line is None:
                    return
                disconnect = await self._handle_control(line.decode("ascii"))
                if disconnect:
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def _handle_control(self, line: str) -> bool:
        """Process one control line. Returns True if the client wants
        the session to disconnect."""
        verb, args = control.parse_control_command(line)
        _logger.debug("CTRL <- %s %s", verb, args)
        writer = self._ctrl[1]
        if verb == control.CMD_PING:
            writer.write(control.RSP_PING)
        elif verb == control.CMD_GET_PARAM:
            name = args[0] if args else ""
            value = self._get_param(name) if name else None
            if value is None:
                _logger.warning("GET_PARAM %r — unknown parameter, "
                                "replying with GET_PARAM_FAILURE", name)
                writer.write(control.RSP_GET_PARAM_FAIL)
            else:
                writer.write(value.encode("ascii") + b"\0")
        elif verb == control.CMD_SET_PARAM:
            if len(args) >= 2 and self._set_param(args[0], args[1]):
                # Accepted but ignored — log so we know what tools try
                # to set on us and can decide if it's worth honouring.
                _logger.info("SET_PARAM %s=%r — accepted but not honoured "
                             "(parameter is read-only in this server)",
                             args[0], args[1])
                writer.write(control.RSP_SET_PARAM_ACK)
            else:
                _logger.warning("SET_PARAM %r — unknown parameter or bad "
                                "syntax, replying with SET_PARAM_FAIL_ACK",
                                args)
                writer.write(control.RSP_SET_PARAM_FAIL)
        elif verb == control.CMD_GET_DRIVER_PARAM:
            name = args[0] if args else ""
            value = self._get_driver_param(name) if name else None
            if value is None:
                _logger.warning("GET_DRIVER_PARAM %r — unknown, replying "
                                "with GET_PARAM_FAILURE", name)
                writer.write(control.RSP_GET_PARAM_FAIL)
            else:
                writer.write(value.encode("ascii") + b"\0")
        elif verb == control.CMD_SET_DRIVER_PARAM:
            if len(args) >= 2 and self._set_driver_param(args[0], args[1]):
                writer.write(control.RSP_SET_PARAM_ACK)
            else:
                _logger.warning("SET_DRIVER_PARAM %r — unknown driver "
                                "parameter, replying with SET_PARAM_FAIL_ACK",
                                args)
                writer.write(control.RSP_SET_PARAM_FAIL)
        elif verb == control.CMD_DISCONNECT:
            _logger.info("DISCONNECT requested by client")
            writer.write(control.RSP_DISCONNECT)
            await writer.drain()
            return True
        else:
            _logger.warning("CTRL: unrecognised command %r (full line %r)",
                            verb, line)
            writer.write(control.RSP_UNRECOGNIZED)
        await writer.drain()
        return False

    def _get_param(self, name: str) -> str | None:
        if name == control.PARAM_MGMT_SUPPORT:
            return "1" if self._mgmt_support else "0"
        if name == control.PARAM_H2T_RX_BUFF_SZ:
            return str(control.DEFAULT_H2T_BUFF_SZ)
        if name == control.PARAM_MGMT_RX_BUFF_SZ:
            return str(control.DEFAULT_MGMT_BUFF_SZ)
        if name == control.PARAM_CTRL_RX_BUFF_SZ:
            return str(control.DEFAULT_CTRL_BUFF_SZ)
        if name == control.PARAM_T2H_NAGLE:
            return "0"
        if name == control.PARAM_MGMT_RSP_NAGLE:
            return "0"
        if name == control.PARAM_SERVER_LOOPBACK:
            return "0"
        return None

    def _set_param(self, name: str, value: str) -> bool:
        if name == control.PARAM_SERVER_LOOPBACK:
            self._server_loopback = (value.strip() == "1")
            _logger.info("SERVER_LOOPBACK %s",
                         "enabled" if self._server_loopback else "disabled")
            return True
        # Other known parameters are advertised statically; accept the
        # write but don't actually mutate state (Nagle, buffer sizes —
        # tools that probe these expect ACK).
        return self._get_param(name) is not None

    def _get_driver_param(self, name: str) -> str | None:
        return self._driver_params.get(name)

    def _set_driver_param(self, name: str, value: str) -> bool:
        if name == "#HW_LOOPBACK":
            self._hw_loopback = (value.strip() == "1")
            self._driver_params[name] = value
            _logger.info("#HW_LOOPBACK %s",
                         "enabled" if self._hw_loopback else "disabled")
            return True
        # Unknown driver params are silently rejected — the caller logs.
        return False

    # --- MGMT drain ---

    async def _mgmt_drain_loop(self) -> None:
        """Read MGMT packets and warn about each one.

        We advertise ``MGMT_SUPPORT=0`` so a well-behaved Quartus skips
        this channel entirely. The on-chip jop_blaster IP also wires the
        MGMT FIFOs to a fixed idle, so they're inert in practice. If
        something *does* arrive here, surface it loudly — it likely means
        a tool we haven't seen before is exercising the side-channel.
        """
        reader, _ = self._mgmt
        try:
            while not self._stop_event.is_set():
                pkt = await framing.read_mgmt_packet(reader)
                _logger.warning(
                    "MGMT packet received unexpectedly: %d bytes "
                    "(channel=%d sop=%d eop=%d): %s — server advertised "
                    "MGMT_SUPPORT=0 and has no MGMT decoder; payload "
                    "ignored, no MGMT_RSP will be generated.",
                    len(pkt.payload), pkt.channel,
                    pkt.sop, pkt.eop, pkt.payload.hex())
        except (asyncio.IncompleteReadError, ConnectionResetError,
                framing.GuardbandError):
            return

    # --- Data plane ---

    async def _h2t_loop(self) -> None:
        reader, _ = self._h2t
        try:
            while not self._stop_event.is_set():
                pkt = await framing.read_h2t_packet(reader)
                _logger.debug("H2T %d bytes (channel=%d sop=%d eop=%d): %s",
                              len(pkt.payload), pkt.channel,
                              pkt.sop, pkt.eop, pkt.payload.hex())
                if self._loopback_active:
                    await self._echo_to_t2h(pkt)
                    continue
                try:
                    ops = self._decoder.feed(pkt.payload)
                except ValueError as e:
                    # Malformed JoP byte stream — surface it but don't
                    # take down the H2T task; reset the decoder buffer so
                    # subsequent valid packets can resync.
                    _logger.warning(
                        "H2T decoder error: %s — payload=%s, dropping "
                        "packet and resetting decoder state.",
                        e, pkt.payload.hex())
                    self._decoder = bs.JopDecoder()
                    continue
                for op in ops:
                    await self._dispatch_op(op, pkt.channel, pkt.conn_id)
        except (asyncio.IncompleteReadError, ConnectionResetError,
                framing.GuardbandError):
            return

    async def _echo_to_t2h(self, pkt: framing.H2tPacket) -> None:
        """Loopback path: re-emit the H2T packet on T2H verbatim
        (preserving SOP/EOP, conn_id, channel, payload). Mirrors Intel's
        reference implementation when ``loopback_mode`` is set."""
        _logger.debug("T2H (loopback) %d bytes (channel=%d sop=%d eop=%d)",
                      len(pkt.payload), pkt.channel, pkt.sop, pkt.eop)
        _, writer = self._t2h
        out = framing.H2tPacket(
            sop=pkt.sop, eop=pkt.eop, conn_id=pkt.conn_id,
            channel=pkt.channel, payload=pkt.payload)
        writer.write(out.encode())
        await writer.drain()

    async def _dispatch_op(self, op: bs.Op,
                           channel: int, conn_id: int) -> None:
        if isinstance(op, bs.RetrieveInfo):
            await self._send_t2h(
                bytes([bs.CONFIG_INFO_RESPONSE_BYTE]),
                channel, conn_id, eop=True)
        elif isinstance(op, bs.ResetTdoFifo):
            self._capture_fifo.clear()
            self._head_remaining = 0
            self._eop_pending = False
        elif isinstance(op, bs.PushTdoCapture):
            if len(self._capture_fifo) >= bs.TDO_FIFO_DEPTH:
                _logger.warning(
                    "TDO-enable FIFO overflow — dropping oldest entry")
                self._capture_fifo.popleft()
            self._capture_fifo.append(op)
            if self._head_remaining == 0 and self._capture_fifo:
                self._head_remaining = self._capture_fifo[0].duration
        elif isinstance(op, bs.Shift):
            await self._do_shift(op, channel, conn_id)

    async def _do_shift(self, op: bs.Shift, channel: int, conn_id: int) -> None:
        tdo = await self._walker.process(op.tms, op.tdi)
        # Accumulated bytes from completed/partial capture windows for
        # this shift, plus a flag tracking whether any completed window
        # asked for EOP.
        out = bytearray()
        any_eop = False
        captured_bits = BitString()

        for i in range(len(tdo)):
            if self._head_remaining == 0:
                continue
            head = self._capture_fifo[0]
            if head.tdo_enable:
                captured_bits += BitString(int(tdo[i]), 1)
            self._head_remaining -= 1
            if self._head_remaining == 0:
                if captured_bits and head.tdo_enable:
                    chunk, _marks = self._encoder.emit_window(
                        captured_bits, eop=head.eop_gen)
                    out.extend(chunk)
                    captured_bits = BitString()
                if head.eop_gen:
                    any_eop = True
                self._capture_fifo.popleft()
                if self._capture_fifo:
                    self._head_remaining = self._capture_fifo[0].duration

        # Bits captured for a still-open window are held in the encoder.
        if captured_bits:
            chunk, _ = self._encoder.emit_window(captured_bits, eop=False)
            out.extend(chunk)

        if out:
            await self._send_t2h(bytes(out), channel, conn_id, eop=any_eop)

    async def _send_t2h(self, payload: bytes, channel: int, conn_id: int,
                        *, eop: bool) -> None:
        _logger.debug("T2H %d bytes (channel=%d sop=1 eop=%d): %s",
                      len(payload), channel, int(eop), payload.hex())
        pkt = framing.H2tPacket(
            sop=True, eop=eop, conn_id=conn_id,
            channel=channel, payload=payload)
        _, writer = self._t2h
        writer.write(pkt.encode())
        await writer.drain()

    # --- Misc ---

    @staticmethod
    async def _read_until_nul(reader: asyncio.StreamReader,
                              max_len: int = 4096) -> bytes | None:
        """Read until NUL or EOF. Returns the line without the NUL,
        or None on EOF."""
        try:
            data = await reader.readuntil(b"\0")
        except asyncio.IncompleteReadError:
            return None
        return data[:-1]


