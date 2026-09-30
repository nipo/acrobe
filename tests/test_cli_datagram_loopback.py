"""`acrobe test datagram-loopback` — validation and statistics against
an in-memory echo datagram, plus an end-to-end CLI run over UDP."""

import asyncio

import asyncclick as click
import pytest

from acrobe.cli import base
from acrobe.cli.test import SIZE, DatagramLoopbackTest
from acrobe.protocol.datagram import Datagram, Recv, Send


class EchoDatagram(Datagram):
    """Echoes every sent message, optionally mangling the n-th one
    (0-based) according to `fault`."""

    def __init__(self, fault=None, fault_index=None):
        super().__init__("echo")
        self.fault = fault
        self.fault_index = fault_index
        self.queue = asyncio.Queue()
        self.held = None
        self.index = 0

    def __echo(self, data):
        index = self.index
        self.index += 1
        if index != self.fault_index:
            self.queue.put_nowait(data)
            if self.held is not None:
                self.queue.put_nowait(self.held)
                self.held = None
            return
        if self.fault == "drop":
            return
        if self.fault == "corrupt":
            data = data[:-1] + bytes([data[-1] ^ 0x01])
        elif self.fault == "truncate":
            data = data[:-1]
        elif self.fault == "duplicate":
            self.queue.put_nowait(data)
        elif self.fault == "reorder":
            self.held = data
            return
        self.queue.put_nowait(data)

    async def __recv(self, future):
        data = await self.queue.get()
        if not future.done():
            future.set_result((data, None))

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Send):
                self.__echo(op.data)
                future.set_result(None)
            elif isinstance(op, Recv):
                asyncio.create_task(self.__recv(future))
            else:
                raise AssertionError(op)


class TestSize:
    @pytest.mark.parametrize("text,value", [
        ("4096", 4096), ("4k", 4096), ("4K", 4096), ("1M", 1 << 20),
        ("2g", 2 << 30), ("0x10", 16),
    ])
    def test_parse(self, text, value):
        assert SIZE.convert(text, None, None) == value

    @pytest.mark.parametrize("text", ["", "k", "abc", "-1", "1.5M"])
    def test_reject(self, text):
        with pytest.raises(click.BadParameter):
            SIZE.convert(text, None, None)


class TestLoopback:
    @pytest.mark.parametrize("window", [1, 8])
    async def test_clean(self, window):
        test = DatagramLoopbackTest(
            EchoDatagram(), count=100, min_size=4, max_size=300,
            window=window, seed=1)
        assert await test.run() is None
        stats = test.stats
        assert stats.sent_packets == 100
        assert stats.valid_packets == 100
        assert stats.valid_bytes == stats.sent_bytes
        assert stats.error_count == 0
        assert stats.reordered == 0
        assert len(stats.latencies) == 100
        assert any(line.startswith("latency:") for line in stats.report())

    async def test_volume_limit(self):
        test = DatagramLoopbackTest(
            EchoDatagram(), volume=1000, min_size=100, max_size=100)
        assert await test.run() is None
        assert test.stats.sent_packets == 10
        assert test.stats.sent_bytes == 1000

    async def test_count_and_volume_first_wins(self):
        test = DatagramLoopbackTest(
            EchoDatagram(), count=3, volume=1000, min_size=100,
            max_size=100)
        assert await test.run() is None
        assert test.stats.sent_packets == 3

    @pytest.mark.parametrize("fault,kind", [
        ("corrupt", "corrupt"),
        ("truncate", "length"),
        ("duplicate", "unexpected"),
    ])
    async def test_fault_stops(self, fault, kind):
        test = DatagramLoopbackTest(
            EchoDatagram(fault, 5), count=20, window=4)
        failure = await test.run()
        assert failure is not None
        assert test.stats.errors == {kind: 1}
        assert test.stats.valid_packets < 20

    @pytest.mark.parametrize("fault,kind", [
        ("corrupt", "corrupt"),
        ("truncate", "length"),
    ])
    async def test_fault_keep_going(self, fault, kind):
        test = DatagramLoopbackTest(
            EchoDatagram(fault, 5), count=20, keep_going=True)
        assert await test.run() is None
        assert test.stats.errors == {kind: 1}
        assert test.stats.valid_packets == 19
        assert test.stats.error_count == 1

    async def test_drop_times_out(self):
        test = DatagramLoopbackTest(
            EchoDatagram("drop", 5), count=20, timeout=0.05)
        failure = await test.run()
        assert "no message received" in str(failure)
        assert test.stats.valid_packets == 5

    async def test_reorder_counted(self):
        test = DatagramLoopbackTest(
            EchoDatagram("reorder", 5), count=20, window=4)
        assert await test.run() is None
        assert test.stats.reordered == 1
        assert test.stats.valid_packets == 20
        assert test.stats.error_count == 0

    def test_rejects_bad_sizes(self):
        with pytest.raises(ValueError):
            DatagramLoopbackTest(EchoDatagram(), count=1, min_size=3)
        with pytest.raises(ValueError):
            DatagramLoopbackTest(EchoDatagram(), count=1, min_size=10,
                                 max_size=9)


class UdpEchoProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(data, addr)


async def test_cli_over_udp(capsys):
    from acrobe.adapter.model import HwRoot
    from acrobe.adapter.udp import UdpEnumerator

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        UdpEchoProtocol, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]

    root = HwRoot()
    root.add_enumerator(UdpEnumerator())

    class UdpOnlyContext(base.CliContext):
        """Avoids the USB/TTY scans of the default HwRoot."""
        hw_root = root

    cli_ctx = UdpOnlyContext()
    cli_ctx.chained = True
    try:
        await base.cli.main(
            args=["test", "datagram-loopback",
                  "-r", f"udp/127.0.0.1:{port}",
                  "-V", "64k", "--min-size", "16", "--max-size", "1024",
                  "-w", "4", "--seed", "3"],
            prog_name="acrobe", obj=cli_ctx, standalone_mode=False)
    finally:
        transport.close()
    out = capsys.readouterr().out
    assert "seed 3" in out
    assert "errors:     0" in out
    assert "throughput:" in out
