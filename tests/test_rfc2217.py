"""RFC 2217: ComPortClient ↔ ComPortServer end-to-end over a memory pipe."""

import asyncio
import pytest

from acrobe.protocol.pipe import Pipe, Read, Write
from acrobe.protocol.serial import (
    SerialPort, SerialConfig, Parity, StopBits, FlowControl, Signals,
    LineState,
)
from acrobe.protocol.telnet import TelnetPipe
from acrobe.rfc2217 import ComPortClient, ComPortServer


class LoopbackPipe(Pipe):
    def __init__(self, rx: asyncio.Queue, tx: asyncio.Queue,
                 name: str = "loopback"):
        super().__init__(name)
        self._rx = rx
        self._tx = tx

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Write):
                try:
                    for b in op.data:
                        await self._tx.put(b)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                    continue
                if not future.done():
                    future.set_result(None)
            elif isinstance(op, Read):
                asyncio.create_task(self._read_task(op.size, future))
            else:
                if not future.done():
                    future.set_exception(TypeError(
                        f"LoopbackPipe: unsupported op {type(op).__name__}"))

    async def _read_task(self, size, future):
        try:
            out = bytearray()
            for _ in range(size):
                out.append(await self._rx.get())
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            return
        if not future.done():
            future.set_result(bytes(out))

    @classmethod
    def pair(cls):
        q1 = asyncio.Queue()
        q2 = asyncio.Queue()
        return cls(q1, q2), cls(q2, q1)


class FakeSerial(SerialPort):
    """In-memory serial port for testing."""

    def __init__(self, name="fake"):
        super().__init__(name)
        self._rx = asyncio.Queue()  # bytes injected (read() takes from here)
        self._tx = bytearray()       # bytes written (write() appends here)
        self._cfg = SerialConfig()
        self._break = False
        self._dtr = True
        self._rts = True
        self._signals = Signals()
        self._flushed = []

    async def write(self, data):
        self._tx.extend(data)

    async def read(self, size):
        out = bytearray()
        for _ in range(size):
            out.append(await self._rx.get())
        return bytes(out)

    async def config_set(self, cfg):
        self._cfg = cfg
        return cfg

    async def config_get(self):
        return self._cfg

    async def break_set(self, on):
        self._break = on

    async def dtr_set(self, on):
        self._dtr = on

    async def rts_set(self, on):
        self._rts = on

    async def signals_get(self):
        return self._signals

    async def flush(self, tx=True, rx=True):
        self._flushed.append((tx, rx))

    # helper for tests: simulate incoming bytes on the "wire"
    async def inject(self, data: bytes):
        for b in data:
            await self._rx.put(b)


async def _make_pair():
    """Set up a ComPortClient and ComPortServer bridged by two telnet pipes."""
    pipe_c, pipe_s = LoopbackPipe.pair()
    telnet_c = TelnetPipe(pipe_c)
    telnet_s = TelnetPipe(pipe_s)
    serial = FakeSerial()
    # Server must be started first so it's ready to receive client's WILL
    server = ComPortServer(telnet_s, serial)
    client = ComPortClient(telnet_c, name="rfc2217")
    await server.start()
    await client.start()
    # Let negotiation settle
    await asyncio.sleep(0.05)
    return client, server, serial


async def test_config_set_roundtrip():
    client, server, serial = await _make_pair()
    cfg = SerialConfig(baud=115200, data_bits=8, parity=Parity.NONE,
                       stop_bits=StopBits.ONE,
                       flow_control=FlowControl.RTS_CTS)
    applied = await client.config_set(cfg)
    assert applied == cfg
    assert serial._cfg == cfg
    await server.stop()


async def test_config_get():
    client, server, serial = await _make_pair()
    serial._cfg = SerialConfig(baud=57600, data_bits=7, parity=Parity.EVEN,
                               stop_bits=StopBits.TWO,
                               flow_control=FlowControl.XON_XOFF)
    got = await client.config_get()
    assert got == serial._cfg
    await server.stop()


async def test_break_dtr_rts():
    client, server, serial = await _make_pair()
    await client.break_set(True);  assert serial._break is True
    await client.break_set(False); assert serial._break is False
    await client.dtr_set(False);   assert serial._dtr is False
    await client.dtr_set(True);    assert serial._dtr is True
    await client.rts_set(False);   assert serial._rts is False
    await client.rts_set(True);    assert serial._rts is True
    await server.stop()


async def test_purge():
    client, server, serial = await _make_pair()
    await client.flush(tx=True, rx=False)
    await client.flush(tx=False, rx=True)
    await client.flush(tx=True, rx=True)
    assert serial._flushed == [(True, False), (False, True), (True, True)]
    await server.stop()


async def test_data_bridging_client_to_server():
    client, server, serial = await _make_pair()
    await client.write(b"hello\xff world")  # IAC byte in payload
    # Server pumps to serial.write; wait briefly
    for _ in range(200):
        if bytes(serial._tx) == b"hello\xff world":
            break
        await asyncio.sleep(0.001)
    assert bytes(serial._tx) == b"hello\xff world"
    await server.stop()


async def test_data_bridging_server_to_client():
    client, server, serial = await _make_pair()
    await serial.inject(b"incoming\xff bytes")
    got = await client.read(len(b"incoming\xff bytes"))
    assert got == b"incoming\xff bytes"
    await server.stop()


async def test_signals_notify():
    client, server, serial = await _make_pair()
    seen = []
    client.on_signals(lambda old, new: seen.append((old, new)))
    # Simulate a signal change on the server's serial port
    new_sig = Signals(cts=True, dsr=True, ri=False, dcd=True)
    old_sig = serial._signals
    serial._signals = new_sig
    serial._emit_signals(old_sig, new_sig)
    # Notify travels through telnet → client; wait briefly
    for _ in range(200):
        if seen:
            break
        await asyncio.sleep(0.001)
    assert seen, "client should have received a signal notification"
    new = seen[-1][1]
    assert new == new_sig
    await server.stop()
