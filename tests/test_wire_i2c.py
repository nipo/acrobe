"""End-to-end: an I2C Interface hosted by aiohttp, accessed remotely.

Mirrors test_wire_swd.py for the Transfer / WaitAck / Transaction op
set. The server-side endpoint is a synthetic `_LoopbackI2c` — no
hardware. Read Transfers resolve to bytes derived from the slave
address so results can be asserted on, and three configured addresses
route to AddressNack / DataNack / WaitAckTimeout so typed error
transport is covered.

Transaction also exercises the union codec: `Transaction.items` is a
`tuple[Transfer | WaitAck, ...]`, so each item crosses the wire as a
tagged union variant.
"""

import textwrap

import pytest
from aiohttp.test_utils import TestClient, TestServer

from acrobe.adapter.model import HwRoot
from acrobe.configuration import Configuration
from acrobe.node import Node
from acrobe.protocol import i2c
from acrobe.wire import WireEnumerator, default_registry
from acrobe.wire.client import RemoteBatcher, WireClient
from acrobe.wire.server import make_app


NACK_ADDR = 0x11
DATA_NACK_ADDR = 0x22
BUSY_ADDR = 0x33


def _expected_read(addr: int, size: int) -> bytes:
    return bytes((addr + index) & 0xff for index in range(size))


class _LoopbackI2c(i2c.Interface):
    """Synthetic I2C Interface used as the server-side endpoint.

    Owns the wire itself, so it overrides `flush_ops` and honours the
    normalize() contract by hand rather than forwarding to a lower
    adapter.
    """

    def __init__(self, name="loopback-i2c"):
        super().__init__(None, name=name)
        self.transactions: list[i2c.Transaction] = []

    async def flush_ops(self, batch):
        for op, future in batch:
            transaction, single = self.normalize(op)
            self.transactions.append(transaction)
            try:
                results = [self.item_run(item)
                           for item in transaction.items]
            except Exception as exc:
                future.set_exception(exc)
                continue
            future.set_result(results[0] if single else results)

    @staticmethod
    def item_run(item):
        if isinstance(item, i2c.Transfer):
            if item.addr == NACK_ADDR:
                raise i2c.AddressNack(item.addr)
            if item.addr == DATA_NACK_ADDR:
                raise i2c.DataNack(item.addr)
            if item.size_r:
                return _expected_read(item.addr, item.size_r)
            return None
        if isinstance(item, i2c.WaitAck):
            if item.addr == BUSY_ADDR:
                raise i2c.WaitAckTimeout(item.addr, item.timeout_s)
            return None
        raise TypeError(f"_LoopbackI2c cannot lower {item!r}")


def _serve(iface):
    root = Node("root")
    root.child_add(iface)
    return make_app(root)


@pytest.mark.asyncio
async def test_remote_transfer_shapes():
    """Write-only, read-only and write-then-read Transfers round-trip."""
    iface = _LoopbackI2c()
    async with TestClient(TestServer(_serve(iface))) as cli:
        url = str(cli.make_url("/v1/node/loopback-i2c"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            assert await proxy.post(
                i2c.Transfer(0x50, data_w=b"\x01\x02")) is None
            assert await proxy.post(i2c.Transfer(0x50, size_r=4)) \
                == _expected_read(0x50, 4)
            assert await proxy.post(
                i2c.Transfer(0x60, data_w=b"\x00", size_r=3)) \
                == _expected_read(0x60, 3)
        finally:
            await client.close()

    assert [t.items[0] for t in iface.transactions] == [
        i2c.Transfer(0x50, data_w=b"\x01\x02"),
        i2c.Transfer(0x50, size_r=4),
        i2c.Transfer(0x60, data_w=b"\x00", size_r=3),
    ]


@pytest.mark.asyncio
async def test_remote_wait_ack_success():
    """A WaitAck that the slave answers resolves to None, with the
    optional interval_s surviving the Optional codec path."""
    iface = _LoopbackI2c()
    async with TestClient(TestServer(_serve(iface))) as cli:
        url = str(cli.make_url("/v1/node/loopback-i2c"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)
            assert await proxy.post(i2c.WaitAck(0x50, 0.05)) is None
            assert await proxy.post(
                i2c.WaitAck(0x50, 0.05, interval_s=0.001)) is None
        finally:
            await client.close()

    waits = [t.items[0] for t in iface.transactions]
    assert [(w.addr, w.timeout_s, w.interval_s) for w in waits] == [
        (0x50, 0.05, None),
        (0x50, 0.05, 0.001),
    ]


@pytest.mark.asyncio
async def test_remote_transaction_mixes_item_types():
    """A Transaction carrying both item types crosses the union codec
    intact and each item's result lands at its own index."""
    iface = _LoopbackI2c()
    transaction = i2c.Transaction((
        i2c.Transfer(0x50, data_w=b"\x00\x10"),
        i2c.WaitAck(0x50, 0.05),
        i2c.Transfer(0x50, size_r=4),
    ))
    async with TestClient(TestServer(_serve(iface))) as cli:
        url = str(cli.make_url("/v1/node/loopback-i2c"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            remote = await RemoteBatcher(client).post(transaction)
        finally:
            await client.close()

    assert remote == [None, None, _expected_read(0x50, 4)]
    # Same op posted locally gives the same result type and values.
    assert await iface.post(transaction) == remote
    assert iface.transactions[0].items == transaction.items


@pytest.mark.asyncio
async def test_remote_errors_arrive_typed():
    """Each I2C error class survives the wire with its fields."""
    iface = _LoopbackI2c()
    async with TestClient(TestServer(_serve(iface))) as cli:
        url = str(cli.make_url("/v1/node/loopback-i2c"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            proxy = RemoteBatcher(client)

            with pytest.raises(i2c.AddressNack) as info:
                await proxy.post(i2c.Transfer(NACK_ADDR, size_r=1))
            assert info.value.addr == NACK_ADDR

            with pytest.raises(i2c.DataNack) as info:
                await proxy.post(
                    i2c.Transfer(DATA_NACK_ADDR, data_w=b"\xaa"))
            assert info.value.addr == DATA_NACK_ADDR

            with pytest.raises(i2c.WaitAckTimeout) as info:
                await proxy.post(i2c.WaitAck(BUSY_ADDR, 0.25))
            assert (info.value.addr, info.value.timeout_s) \
                == (BUSY_ADDR, 0.25)

            # A failing item aborts the rest of its Transaction only.
            with pytest.raises(i2c.AddressNack):
                await proxy.post(i2c.Transaction((
                    i2c.Transfer(NACK_ADDR, size_r=1),
                    i2c.Transfer(0x50, size_r=1),
                )))
            assert await proxy.post(i2c.Transfer(0x50, size_r=2)) \
                == _expected_read(0x50, 2)
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_enumerator_builds_i2c_proxy(tmp_path):
    """Summoning the bus through the wire enumerator yields a proxy
    that IS-A i2c.Interface even though the constructor takes a
    required `adapter` — the registered init hook supplies it."""
    iface = _LoopbackI2c()
    cfg = tmp_path / "acrobe.conf"
    async with TestServer(_serve(iface)) as server:
        cfg.write_text(textwrap.dedent(f"""
            wire:
              servers:
                srv:
                  base: {str(server.make_url("/"))}
        """).strip())
        local = HwRoot()
        local.add_enumerator(
            WireEnumerator(configuration=Configuration(path=cfg)))
        await local.ensure_started()
        try:
            proxy = await local.child_summon("wire", "srv", "loopback-i2c")
            assert isinstance(proxy, i2c.Interface)
            assert proxy.name == "loopback-i2c"
            assert await proxy.post(i2c.Transfer(0x50, size_r=2)) \
                == _expected_read(0x50, 2)
            slave = i2c.Slave(proxy, addr=0x50)
            assert await slave.write_read(b"\x00", 3) \
                == _expected_read(0x50, 3)
        finally:
            await local.stop_tree()
