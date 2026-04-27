"""TelnetPipe: IAC framing, data escape, option dispatch, auto-refuse."""

import asyncio
import pytest

from acrobe.protocol.pipe import Pipe
from acrobe.protocol.telnet import (
    TelnetPipe, TelnetOption, IAC, DO, DONT, WILL, WONT, SB, SE,
)


class LoopbackPipe(Pipe):
    """Bidirectional in-memory byte pipe: two ends share two queues."""

    def __init__(self, rx: asyncio.Queue, tx: asyncio.Queue):
        self._rx = rx
        self._tx = tx

    async def write(self, data: bytes) -> None:
        for b in data:
            await self._tx.put(b)

    async def read(self, size: int) -> bytes:
        out = bytearray()
        for _ in range(size):
            out.append(await self._rx.get())
        return bytes(out)

    @classmethod
    def make_pair(cls):
        q1 = asyncio.Queue()
        q2 = asyncio.Queue()
        return cls(q1, q2), cls(q2, q1)


async def test_data_passthrough_escapes_iac():
    a, b = LoopbackPipe.make_pair()
    ta = TelnetPipe(a)
    tb = TelnetPipe(b)
    ta.start(); tb.start()

    payload = b"hello\xffworld"  # contains IAC byte
    await ta.write(payload)
    got = await tb.read(len(payload))
    assert got == payload
    await ta.close(); await tb.close()


async def test_unknown_option_auto_refused():
    a, b = LoopbackPipe.make_pair()
    ta = TelnetPipe(a)
    tb = TelnetPipe(b)
    ta.start(); tb.start()

    # a says IAC DO 99 — an option b has not registered
    await a.write(bytes([IAC, DO, 99]))
    # b's reader should eventually emit IAC WONT 99 back through b→a.
    reply = await a.read(3)
    assert reply == bytes([IAC, WONT, 99])
    await ta.close(); await tb.close()


async def test_unknown_will_autorefused_with_dont():
    a, b = LoopbackPipe.make_pair()
    ta = TelnetPipe(a)
    tb = TelnetPipe(b)
    ta.start(); tb.start()

    await a.write(bytes([IAC, WILL, 77]))
    reply = await a.read(3)
    assert reply == bytes([IAC, DONT, 77])
    await ta.close(); await tb.close()


async def test_option_dispatch_on_do_with_handler():
    class Opt(TelnetOption):
        code = 44
        name = "comport"
        def __init__(self):
            self.did = False
        async def peer_do(self, telnet):
            self.did = True

    a, b = LoopbackPipe.make_pair()
    ta = TelnetPipe(a)
    tb = TelnetPipe(b)
    opt = Opt()
    tb.option_add(opt)
    ta.start(); tb.start()

    await a.write(bytes([IAC, DO, 44]))
    # Give b's reader a chance to dispatch
    for _ in range(100):
        if opt.did:
            break
        await asyncio.sleep(0.001)
    assert opt.did
    await ta.close(); await tb.close()


async def test_sb_dispatch_escape():
    class Opt(TelnetOption):
        code = 44
        def __init__(self):
            self.payload = None
        async def peer_sb(self, telnet, payload):
            self.payload = payload

    a, b = LoopbackPipe.make_pair()
    ta = TelnetPipe(a)
    tb = TelnetPipe(b)
    opt = Opt()
    tb.option_add(opt)
    ta.start(); tb.start()

    # SB with embedded IAC-escaped byte (0xff represented as 0xff 0xff)
    await a.write(bytes([IAC, SB, 44, 0x01, IAC, IAC, 0x02, IAC, SE]))
    for _ in range(100):
        if opt.payload is not None:
            break
        await asyncio.sleep(0.001)
    assert opt.payload == bytes([0x01, 0xff, 0x02])
    await ta.close(); await tb.close()
