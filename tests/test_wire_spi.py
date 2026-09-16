"""End-to-end: a spi.Interface hosted by aiohttp, driven remotely.

Covers the raw op round trip (Cs / Shift, including a non
byte-aligned shift), a local `spi.Target` stacked on a remote
interface, and the enumerator path that materialises a proxy which
IS-A `spi.Interface`.

The server endpoint is a synthetic `_LoopbackSpi`: it answers a
reading shift with the bitwise inverse of MOSI and records batch
boundaries so a test can assert that a chip-select run stayed
whole.
"""

import textwrap

import pytest
from aiohttp.test_utils import TestClient, TestServer

from acrobe.adapter.model import HwRoot
from acrobe.bitstring import BitString
from acrobe.configuration import Configuration
from acrobe.node import Node
from acrobe.protocol import spi
from acrobe.wire import WireEnumerator, default_registry
from acrobe.wire.client import RemoteBatcher, WireClient
from acrobe.wire.server import make_app


class _LoopbackSpi(spi.Interface):
    """Synthetic SPI interface used as the server-side endpoint.

    `ops` accumulates every op seen, `batches` keeps one list per
    flush so callers can check how ops were grouped.
    """

    def __init__(self, name="loopback"):
        super().__init__(adapter=None, name=name)
        self.ops: list = []
        self.batches: list[list] = []

    async def flush_ops(self, batch):
        self.batches.append([op for op, _ in batch])
        for op, future in batch:
            self.ops.append(op)
            if isinstance(op, spi.Shift) and op.read_miso:
                bits = len(op.mosi)
                future.set_result(
                    BitString(((1 << bits) - 1) - int(op.mosi), bits))
            else:
                future.set_result(None)


def _inverse(value: int, bits: int) -> int:
    return ((1 << bits) - 1) - (value & ((1 << bits) - 1))


async def _connected(iface, coro_body):
    """Serve `iface` over an in-process aiohttp server and hand
    `coro_body` a RemoteBatcher bound to it."""
    root = Node("root")
    root.child_add(iface)
    app = make_app(root)

    async with TestClient(TestServer(app)) as cli:
        url = str(cli.make_url(f"/v1/node/{iface.name}"))
        client = await WireClient.connect(
            url, default_registry(), http_session=cli.session)
        try:
            await coro_body(RemoteBatcher(client))
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_remote_spi_cs_round_trip():
    """Cs assert and release both resolve to None."""
    iface = _LoopbackSpi()

    async def body(proxy):
        assert await proxy.post(spi.Cs(1, mode=3)) is None
        assert await proxy.post(spi.Cs(None)) is None

    await _connected(iface, body)

    assert [type(op).__name__ for op in iface.ops] == ["Cs", "Cs"]
    assert (iface.ops[0].value, iface.ops[0].mode) == (1, 3)
    assert iface.ops[1].value is None


@pytest.mark.asyncio
async def test_remote_spi_shift_forms():
    """Shifts built from bytes, from a byte count, and from a
    non-byte-aligned BitString all round-trip."""
    iface = _LoopbackSpi()

    async def body(proxy):
        f_bytes = proxy.post(spi.Shift(b"\x9f\x00"))
        f_count = proxy.post(spi.Shift(4))
        f_bits = proxy.post(spi.Shift(BitString(0x1abc, 13)))
        f_write = proxy.post(spi.Shift(b"\x06", read_miso=False))

        miso = await f_bytes
        assert isinstance(miso, BitString)
        assert len(miso) == 16
        assert int(miso) == _inverse(0x009f, 16)

        miso = await f_count
        assert len(miso) == 32
        assert int(miso) == 0xffffffff

        miso = await f_bits
        assert len(miso) == 13
        assert int(miso) == _inverse(0x1abc, 13)

        assert await f_write is None

    await _connected(iface, body)

    assert [len(op.mosi) for op in iface.ops] == [16, 32, 13, 8]
    assert [op.read_miso for op in iface.ops] == [True, True, True, False]


@pytest.mark.asyncio
async def test_remote_spi_target_transaction_is_one_batch():
    """A local Target over a remote interface: the CS run reaches
    the server whole, and the transaction resolves per shift."""
    iface = _LoopbackSpi()

    async def body(proxy):
        target = spi.Target(proxy, cs=2, mode=1, name="cs2")
        results = await target.transaction(
            spi.Shift(b"\x9f"),
            spi.Shift(2),
            spi.Shift(b"\x01", read_miso=False))

        assert len(results) == 3
        assert int(results[0]) == _inverse(0x9f, 8)
        assert int(results[1]) == 0xffff
        assert results[2] is None

    await _connected(iface, body)

    assert len(iface.batches) == 1
    batch = iface.batches[0]
    assert [type(op).__name__ for op in batch] == [
        "Cs", "Shift", "Shift", "Shift", "Cs"]
    assert (batch[0].value, batch[0].mode) == (2, 1)
    assert batch[-1].value is None


async def _make_local_root(server_url, tmp_path):
    cfg = tmp_path / "acrobe.conf"
    cfg.write_text(textwrap.dedent(f"""
        wire:
          servers:
            srv:
              base: {server_url}
    """).strip())
    root = HwRoot()
    root.add_enumerator(WireEnumerator(configuration=Configuration(path=cfg)))
    await root.ensure_started()
    return root


@pytest.mark.asyncio
async def test_enumerated_spi_proxy_drives_a_local_target(tmp_path):
    """Summoning the interface through the wire enumerator yields a
    proxy that IS-A spi.Interface; a Target built on it drives the
    remote hardware."""
    iface = _LoopbackSpi(name="spi")
    remote = Node("HwRoot")
    remote.child_add(iface)
    app = make_app(remote)

    async with TestServer(app) as server:
        local = await _make_local_root(str(server.make_url("/")), tmp_path)
        try:
            proxy = await local.child_summon("wire", "srv", "spi")
            assert isinstance(proxy, spi.Interface)
            assert proxy.name == "spi"

            target = spi.Target(proxy, cs=0, mode=0, name="cs0")
            proxy.child_add(target)
            miso, = await target.transaction(spi.Shift(b"\x9f"))
            assert int(miso) == _inverse(0x9f, 8)
        finally:
            await local.stop_tree()

    assert [type(op).__name__ for op in iface.ops] == ["Cs", "Shift", "Cs"]
