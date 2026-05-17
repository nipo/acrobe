"""TCP / UDP generic adapters — loopback round trips through HwRoot."""

import asyncio

import pytest

from acrobe.adapter.model import make_hw_root
from acrobe.adapter.tcp import TcpBroker, TcpPipe
from acrobe.adapter.udp import UdpBroker, UdpDatagram
from acrobe.protocol.datagram import Datagram
from acrobe.protocol.pipe import Pipe


async def _tcp_echo_handler(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter):
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionError):
        return
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _start_tcp_echo():
    server = await asyncio.start_server(_tcp_echo_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_tcp_pipe_roundtrip_via_hw_root():
    server, port = await _start_tcp_echo()
    try:
        root = make_hw_root()
        node = await root.child_summon("tcp", f"127.0.0.1:{port}")
        assert isinstance(node, TcpPipe)
        assert isinstance(node, Pipe)

        await node.write(b"hello")
        assert await node.read(5) == b"hello"

        await node.write(b"world!\n")
        assert await node.read(7) == b"world!\n"

        await node.stop()
    finally:
        server.close()
        await server.wait_closed()


async def test_tcp_broker_rejects_bad_endpoint():
    root = make_hw_root()
    broker = await root.child_summon("tcp")
    assert isinstance(broker, TcpBroker)
    with pytest.raises(Exception):
        await broker.child_summon("not-a-host-port")


async def test_pipe_db_dispatch_via_child_spawn():
    """A handler registered against Pipe.db is reachable as a child."""
    from acrobe.protocol.pipe import Pipe

    class _PipeWrapper(Pipe):
        def __init__(self, transport):
            super().__init__("wrapped")
            self.transport = transport

    Pipe.db.register("wrapped")(_PipeWrapper)
    try:
        server, port = await _start_tcp_echo()
        try:
            root = make_hw_root()
            wrapped = await root.child_summon(
                "tcp", f"127.0.0.1:{port}", "wrapped")
            assert isinstance(wrapped, _PipeWrapper)
            assert isinstance(wrapped.transport, TcpPipe)
            await wrapped.transport.stop()
        finally:
            server.close()
            await server.wait_closed()
    finally:
        Pipe.db.registry.pop("wrapped", None)


# ---------------------------------------------------------------------------
# UDP
# ---------------------------------------------------------------------------


class _UdpEchoProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if self.transport is not None:
            self.transport.sendto(data, addr)


async def _start_udp_echo():
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _UdpEchoProtocol, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    return transport, port


async def test_udp_datagram_roundtrip_via_hw_root():
    transport, port = await _start_udp_echo()
    try:
        root = make_hw_root()
        node = await root.child_summon("udp", f"127.0.0.1:{port}")
        assert isinstance(node, UdpDatagram)
        assert isinstance(node, Datagram)

        await node.send(b"ping")
        data, addr = await asyncio.wait_for(node.recv(), timeout=1.0)
        assert data == b"ping"

        await node.send(b"second")
        data, _ = await asyncio.wait_for(node.recv(), timeout=1.0)
        assert data == b"second"

        await node.stop()
    finally:
        transport.close()


async def test_udp_broker_rejects_bad_endpoint():
    root = make_hw_root()
    broker = await root.child_summon("udp")
    assert isinstance(broker, UdpBroker)
    with pytest.raises(Exception):
        await broker.child_summon("nope")


async def test_datagram_db_dispatch_via_child_spawn():
    from acrobe.protocol.datagram import Datagram

    class _DgramWrapper(Datagram):
        def __init__(self, transport):
            super().__init__("wrapped")
            self.transport = transport

    Datagram.db.register("wrapped")(_DgramWrapper)
    try:
        transport, port = await _start_udp_echo()
        try:
            root = make_hw_root()
            wrapped = await root.child_summon(
                "udp", f"127.0.0.1:{port}", "wrapped")
            assert isinstance(wrapped, _DgramWrapper)
            assert isinstance(wrapped.transport, UdpDatagram)
            await wrapped.transport.stop()
        finally:
            transport.close()
    finally:
        Datagram.db.registry.pop("wrapped", None)
