"""Tests for the I2C memory component, its presets, and its Target."""

import asyncio

import pytest

from acrobe.component.i2c_mem import I2cEeprom, I2cMem
from acrobe.db import NoMatch
from acrobe.engine import Batcher
from acrobe.memory_map import MemoryMap
from acrobe.node import Node
from acrobe.protocol import i2c
from acrobe.protocol.memory import (
    Interface as MemoryInterface, ReadBlob, RegisterFromBulk,
    UnsupportedAccess, WriteBlob,
)
from acrobe.target import Loadable, Target, TargetDiscovery
from acrobe.target.i2c_mem import I2cMemRegion, I2cMemTarget


class FakeDevice:
    """Byte array answering address-prefixed I2C Transfers.

    Address is rebuilt from the slave address (bank bits) and the
    big-endian prefix, exactly as the part would.
    """

    def __init__(self, saddr=0x50, addr_bytes=2, saddr_bits=0,
                 size=0x2000, fill=0xff):
        self.saddr = saddr
        self.addr_bytes = addr_bytes
        self.saddr_bits = saddr_bits
        self.storage = bytearray([fill]) * size

    def __call__(self, items):
        out = []
        for item in items:
            if isinstance(item, i2c.WaitAck):
                out.append(None)
                continue
            bank = item.addr - (self.saddr & ~((1 << self.saddr_bits) - 1))
            assert 0 <= bank < (1 << self.saddr_bits) or bank == 0, \
                f"bad slave address 0x{item.addr:02x}"
            low = int.from_bytes(item.data_w[:self.addr_bytes], "big")
            addr = (bank << (self.addr_bytes * 8)) | low
            payload = item.data_w[self.addr_bytes:]
            if payload:
                self.storage[addr:addr + len(payload)] = payload
                out.append(None)
            elif item.size_r:
                out.append(bytes(self.storage[addr:addr + item.size_r]))
            else:
                out.append(None)
        return tuple(out)


class MockAdapter(Batcher, Node):
    """Records every Transaction and answers through `responder`."""

    def __init__(self, responder=None):
        Batcher.__init__(self)
        Node.__init__(self, "mock-adapter")
        self.transactions = []
        self.responder = responder or (lambda items: tuple(
            bytes(i.size_r) if isinstance(i, i2c.Transfer) and i.size_r
            else None for i in items))

    async def flush_ops(self, batch):
        for op, future in batch:
            assert isinstance(op, i2c.Transaction), f"got {op!r}"
            self.transactions.append(op)
            try:
                future.set_result(self.responder(op.items))
            except Exception as exc:
                future.set_exception(exc)


def make_bus(responder=None):
    adapter = MockAdapter(responder)
    return i2c.Interface(adapter), adapter


async def make_mem(cls=I2cMem, responder=None, **kwargs):
    bus, adapter = make_bus(responder)
    mem = cls(bus, "mem", **kwargs)
    bus.child_add(mem)
    await mem.ensure_started()
    return mem, adapter


def transfers(adapter):
    """Every Transfer item across recorded Transactions, in order."""
    return [item for tx in adapter.transactions for item in tx.items
            if isinstance(item, i2c.Transfer)]


class TestAddressEncoding:
    @pytest.mark.asyncio
    async def test_two_byte_prefix(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=2,
                                      size=0x2000)
        await mem.mem_read(0x1234, 4)
        item = transfers(adapter)[0]
        assert item.addr == 0x50
        assert item.data_w == b"\x12\x34"
        assert item.size_r == 4

    @pytest.mark.asyncio
    async def test_one_byte_prefix(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=1,
                                      size=0x100)
        await mem.mem_read(0xa5, 2)
        assert transfers(adapter)[0].data_w == b"\xa5"

    @pytest.mark.asyncio
    async def test_three_byte_prefix(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=3,
                                      size=0x10000)
        await mem.mem_read(0x00abcd, 1)
        assert transfers(adapter)[0].data_w == b"\x00\xab\xcd"

    @pytest.mark.asyncio
    async def test_saddr_bits_select_bank(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=1,
                                      saddr_bits=2, page_size=16)
        assert mem.size == 0x400
        await mem.mem_read(0x2c0, 1)
        item = transfers(adapter)[0]
        assert item.addr == 0x52
        assert item.data_w == b"\xc0"

    @pytest.mark.asyncio
    async def test_saddr_bits_mask_base_address(self):
        """Low bits of the configured saddr are bank bits, not part of
        the base address."""
        mem, adapter = await make_mem(saddr=0x53, addr_bytes=1,
                                      saddr_bits=2, page_size=16)
        await mem.mem_read(0x100, 1)
        assert transfers(adapter)[0].addr == 0x51

    @pytest.mark.asyncio
    async def test_read_split_at_bank_boundary(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=1,
                                      saddr_bits=2, page_size=16)
        await mem.mem_read(0xfe, 4)
        items = transfers(adapter)
        assert len(items) == 2
        assert (items[0].addr, items[0].data_w, items[0].size_r) == (
            0x50, b"\xfe", 2)
        assert (items[1].addr, items[1].data_w, items[1].size_r) == (
            0x51, b"\x00", 2)

    @pytest.mark.asyncio
    async def test_write_split_at_bank_boundary(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=1,
                                      saddr_bits=2, page_size=16)
        await mem.mem_write(0xf0, bytes(range(32)))
        items = transfers(adapter)
        assert [i.addr for i in items] == [0x50, 0x51]
        assert items[0].data_w == b"\xf0" + bytes(range(16))
        assert items[1].data_w == b"\x00" + bytes(range(16, 32))


class TestChunking:
    @pytest.mark.asyncio
    async def test_write_page_split_aligned(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=2,
                                      size=0x2000, page_size=8)
        await mem.mem_write(0x100, bytes(range(24)))
        items = transfers(adapter)
        assert len(items) == 3
        assert [i.data_w[:2] for i in items] == [
            b"\x01\x00", b"\x01\x08", b"\x01\x10"]
        assert items[0].data_w[2:] == bytes(range(8))

    @pytest.mark.asyncio
    async def test_write_page_split_unaligned_head(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=2,
                                      size=0x2000, page_size=8)
        await mem.mem_write(0x105, bytes(range(12)))
        items = transfers(adapter)
        assert len(items) == 3
        assert [(i.data_w[:2], len(i.data_w) - 2) for i in items] == [
            (b"\x01\x05", 3), (b"\x01\x08", 8), (b"\x01\x10", 1)]

    @pytest.mark.asyncio
    async def test_read_chunking(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=2,
                                      size=0x2000, read_chunk=16)
        await mem.mem_read(0x40, 40)
        items = transfers(adapter)
        assert [i.size_r for i in items] == [16, 16, 8]
        assert [i.data_w for i in items] == [
            b"\x00\x40", b"\x00\x50", b"\x00\x60"]

    @pytest.mark.asyncio
    async def test_read_ignores_page_size(self):
        """Sequential read auto-increments; only read_chunk bounds it."""
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=2,
                                      size=0x2000, page_size=8,
                                      read_chunk=64)
        await mem.mem_read(0, 64)
        assert len(transfers(adapter)) == 1

    @pytest.mark.asyncio
    async def test_zero_size_read(self):
        mem, adapter = await make_mem(saddr=0x50)
        assert await mem.mem_read(0x10, 0) == b""
        assert adapter.transactions == []

    @pytest.mark.asyncio
    async def test_zero_size_write(self):
        mem, adapter = await make_mem(saddr=0x50)
        assert await mem.mem_write(0x10, b"") is None
        assert adapter.transactions == []

    @pytest.mark.asyncio
    async def test_batched_reads_share_one_flush(self):
        mem, adapter = await make_mem(saddr=0x50, addr_bytes=2,
                                      size=0x2000)
        f1 = mem.mem_read(0, 4)
        f2 = mem.mem_read(0x100, 4)
        r1, r2 = await asyncio.gather(f1, f2)
        assert len(r1) == len(r2) == 4
        assert len(adapter.transactions) == 2


class TestFailurePaths:
    @pytest.mark.asyncio
    async def test_chunk_failure_fails_the_blob(self):
        seen = []

        def nack(items):
            seen.append(items)
            if len(seen) == 2:
                raise i2c.AddressNack(items[0].addr)
            return tuple(bytes(i.size_r) if i.size_r else None
                         for i in items)

        mem, _ = await make_mem(responder=nack, saddr=0x50,
                                addr_bytes=2, size=0x2000, read_chunk=8)
        with pytest.raises(i2c.AddressNack):
            await mem.mem_read(0, 32)

    @pytest.mark.asyncio
    async def test_fire_and_forget_write(self):
        device = FakeDevice(saddr=0x50, addr_bytes=2, size=0x2000)
        mem, adapter = await make_mem(responder=device, saddr=0x50,
                                      addr_bytes=2, size=0x2000,
                                      page_size=8)
        mem.post_no_wait(WriteBlob(0, bytes(range(16))))
        await asyncio.sleep(0)
        await mem.mem_read(0, 1)
        assert bytes(device.storage[:16]) == bytes(range(16))
        assert len(adapter.transactions) == 3

    @pytest.mark.asyncio
    async def test_fire_and_forget_failure_does_not_break_the_node(self):
        answers = []

        def nack_once(items):
            answers.append(items)
            if len(answers) == 1:
                raise i2c.AddressNack(items[0].addr)
            return tuple(bytes(i.size_r) if i.size_r else None
                         for i in items)

        mem, _ = await make_mem(responder=nack_once, saddr=0x50)
        mem.post_no_wait(ReadBlob(0, 4))
        for _ in range(3):
            await asyncio.sleep(0)
        assert await mem.mem_read(0, 4) == bytes(4)


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_write_then_read(self):
        device = FakeDevice(saddr=0x50, addr_bytes=2, size=0x2000)
        mem, _ = await make_mem(responder=device, saddr=0x50,
                                addr_bytes=2, size=0x2000, page_size=32)
        payload = bytes(range(256)) * 2
        await mem.mem_write(0x123, payload)
        assert await mem.mem_read(0x123, len(payload)) == payload

    @pytest.mark.asyncio
    async def test_write_then_read_across_banks(self):
        device = FakeDevice(saddr=0x50, addr_bytes=1, saddr_bits=2,
                            size=0x400)
        mem, _ = await make_mem(responder=device, saddr=0x50,
                                addr_bytes=1, saddr_bits=2, page_size=16)
        payload = bytes(range(200))
        await mem.mem_write(0x1a0, payload)
        assert await mem.mem_read(0x1a0, len(payload)) == payload


class TestBounds:
    @pytest.mark.asyncio
    async def test_read_past_end_fails(self):
        mem, _ = await make_mem(saddr=0x50, addr_bytes=2, size=0x100)
        with pytest.raises(ValueError):
            await mem.mem_read(0xfe, 4)

    @pytest.mark.asyncio
    async def test_write_past_end_fails(self):
        mem, _ = await make_mem(saddr=0x50, addr_bytes=2, size=0x100)
        with pytest.raises(ValueError):
            await mem.mem_write(0xff, b"\x01\x02")

    @pytest.mark.asyncio
    async def test_negative_address_fails(self):
        mem, _ = await make_mem(saddr=0x50)
        with pytest.raises(ValueError):
            await mem.mem_read(-1, 1)

    @pytest.mark.asyncio
    async def test_size_beyond_address_range_rejected(self):
        bus, _ = make_bus()
        mem = I2cMem(bus, "mem", saddr=0x50, addr_bytes=1, size=0x200)
        with pytest.raises(ValueError):
            await mem.ensure_started()

    @pytest.mark.asyncio
    async def test_addr_bytes_out_of_range_rejected(self):
        bus, _ = make_bus()
        mem = I2cMem(bus, "mem", saddr=0x50, addr_bytes=5)
        with pytest.raises(ValueError):
            await mem.ensure_started()

    @pytest.mark.asyncio
    async def test_page_size_must_divide_bank(self):
        bus, _ = make_bus()
        mem = I2cMem(bus, "mem", saddr=0x50, addr_bytes=1, page_size=48)
        with pytest.raises(ValueError):
            await mem.ensure_started()

    @pytest.mark.asyncio
    async def test_missing_saddr_fails_at_start(self):
        bus, _ = make_bus()
        mem = I2cMem(bus, "mem")
        with pytest.raises(ValueError):
            await mem.ensure_started()


class TestBulkOnly:
    @pytest.mark.asyncio
    async def test_read32_unsupported(self):
        mem, _ = await make_mem(saddr=0x50)
        with pytest.raises(UnsupportedAccess):
            mem.read32(0)

    @pytest.mark.asyncio
    async def test_write8_unsupported(self):
        mem, _ = await make_mem(saddr=0x50)
        with pytest.raises(UnsupportedAccess):
            mem.write8(0, 0x5a)

    @pytest.mark.asyncio
    async def test_unknown_op_rejected(self):
        mem, _ = await make_mem(saddr=0x50)
        with pytest.raises(TypeError):
            await mem.post("garbage")


class TestReadableWritable:
    @pytest.mark.asyncio
    async def test_read_clamps_at_end(self):
        device = FakeDevice(saddr=0x50, addr_bytes=2, size=0x100)
        mem, _ = await make_mem(responder=device, saddr=0x50,
                                addr_bytes=2, size=0x100)
        data = await mem.read(0xf0, 64)
        assert len(data) == 16

    @pytest.mark.asyncio
    async def test_read_offset_past_size_fails(self):
        mem, _ = await make_mem(saddr=0x50, addr_bytes=2, size=0x100)
        with pytest.raises(ValueError):
            await mem.read(0x101, 1)

    @pytest.mark.asyncio
    async def test_write_bounds_enforced(self):
        mem, _ = await make_mem(saddr=0x50, addr_bytes=2, size=0x100)
        with pytest.raises(ValueError):
            await mem.write(0xff, b"\x01\x02")

    @pytest.mark.asyncio
    async def test_write_read_roundtrip(self):
        device = FakeDevice(saddr=0x50, addr_bytes=2, size=0x100)
        mem, _ = await make_mem(responder=device, saddr=0x50,
                                addr_bytes=2, size=0x100, page_size=16)
        await mem.write(0x20, b"acrobe")
        assert await mem.read(0x20, 6) == b"acrobe"

    @pytest.mark.asyncio
    async def test_addressable(self):
        mem, _ = await make_mem(saddr=0x50, addr_bytes=2, size=0x100)
        assert mem.load_address == 0
        assert mem.addresses == {"load": 0}
        assert mem.size == 0x100

    @pytest.mark.asyncio
    async def test_metadata(self):
        mem, _ = await make_mem(saddr=0x50, addr_bytes=2, size=0x100,
                                page_size=16)
        assert mem.metadata["saddr"] == 0x50
        assert mem.metadata["addr_bytes"] == 2
        assert mem.metadata["size"] == 0x100
        assert mem.metadata["page_size"] == 16


class TestEeprom:
    @pytest.mark.asyncio
    async def test_write_is_gated_by_wait_ack(self):
        mem, adapter = await make_mem(I2cEeprom, saddr=0x50,
                                      addr_bytes=2, size=0x2000,
                                      page_size=8)
        await mem.mem_write(0, bytes(16))
        assert len(adapter.transactions) == 2
        for tx in adapter.transactions:
            assert isinstance(tx.items[0], i2c.WaitAck)
            assert tx.items[0].addr == 0x50
            assert isinstance(tx.items[1], i2c.Transfer)

    @pytest.mark.asyncio
    async def test_read_is_gated_by_wait_ack(self):
        mem, adapter = await make_mem(I2cEeprom, saddr=0x50,
                                      addr_bytes=2, size=0x2000)
        await mem.mem_read(0, 4)
        tx = adapter.transactions[0]
        assert isinstance(tx.items[0], i2c.WaitAck)
        assert tx.items[1].size_r == 4

    @pytest.mark.asyncio
    async def test_wait_ack_uses_banked_slave_address(self):
        mem, adapter = await make_mem(I2cEeprom, saddr=0x50,
                                      addr_bytes=1, saddr_bits=2,
                                      page_size=16)
        await mem.mem_read(0x300, 1)
        assert adapter.transactions[0].items[0].addr == 0x53

    @pytest.mark.asyncio
    async def test_read_result_survives_prefix(self):
        device = FakeDevice(saddr=0x50, addr_bytes=2, size=0x100)
        device.storage[0x10:0x14] = b"\xde\xad\xbe\xef"
        mem, _ = await make_mem(I2cEeprom, responder=device, saddr=0x50,
                                addr_bytes=2, size=0x100)
        assert await mem.mem_read(0x10, 4) == b"\xde\xad\xbe\xef"

    @pytest.mark.asyncio
    async def test_ready_timeout_option(self):
        mem, adapter = await make_mem(I2cEeprom, saddr=0x50)
        mem.option_set("ready_timeout", "0.25")
        await mem.mem_read(0, 1)
        assert adapter.transactions[0].items[0].timeout_s == 0.25

    @pytest.mark.asyncio
    async def test_wait_ack_timeout_propagates(self):
        def refuse(items):
            raise i2c.WaitAckTimeout(items[0].addr, items[0].timeout_s)

        mem, _ = await make_mem(I2cEeprom, responder=refuse, saddr=0x50)
        with pytest.raises(i2c.WaitAckTimeout):
            await mem.mem_write(0, b"\x01")


class TestSpawn:
    @pytest.mark.asyncio
    async def test_child_hints_lists_presets(self):
        bus, _ = make_bus()
        hints = bus.child_hints()
        assert "memory" in hints
        assert "eeprom" in hints
        assert "24lc64" in hints

    @pytest.mark.asyncio
    async def test_summon_generic_memory_with_options(self):
        bus, _ = make_bus()
        mem = await bus.child_summon(
            "memory(saddr=0x50,addr_bytes=2,size=0x2000,page_size=32)")
        assert isinstance(mem, I2cMem)
        assert not isinstance(mem, I2cEeprom)
        assert (mem.saddr, mem.addr_bytes, mem.size, mem.page_size) == (
            0x50, 2, 0x2000, 32)
        assert mem.parent is bus

    @pytest.mark.asyncio
    async def test_summon_generic_eeprom(self):
        bus, _ = make_bus()
        mem = await bus.child_summon("eeprom(saddr=0x50)")
        assert isinstance(mem, I2cEeprom)
        assert mem.addr_bytes == 2
        assert mem.page_size == 16
        assert mem.size == 0x10000

    @pytest.mark.asyncio
    async def test_summon_preset(self):
        bus, _ = make_bus()
        mem = await bus.child_summon("24lc64(saddr=0x50)")
        assert isinstance(mem, I2cEeprom)
        assert (mem.addr_bytes, mem.page_size, mem.size) == (2, 32, 8192)

    @pytest.mark.asyncio
    async def test_summon_preset_with_fixed_saddr(self):
        bus, _ = make_bus()
        mem = await bus.child_summon("24lc08")
        assert (mem.saddr, mem.addr_bytes, mem.saddr_bits,
                mem.page_size, mem.size) == (0x50, 1, 2, 16, 0x400)

    @pytest.mark.asyncio
    async def test_preset_parameters(self):
        expected = {
            "m24m02": (None, 2, 2, 256, 1 << 18),
            "24aa64": (None, 2, 0, 32, 8192),
            "24fc64": (None, 2, 0, 32, 8192),
            "cat24c32": (None, 2, 0, 32, 4096),
            "24lc128": (None, 2, 0, 64, 65536),
            "pca24s08": (0x54, 1, 2, 16, 0x400),
            "pca24s08_prot": (0x5c, 1, 0, 1, 32),
        }
        for name, (saddr, addr_bytes, saddr_bits, page, size) in \
                expected.items():
            bus, _ = make_bus()
            mem = await bus.child_summon(
                name if saddr is not None else f"{name}(saddr=0x50)")
            assert mem.addr_bytes == addr_bytes, name
            assert mem.saddr_bits == saddr_bits, name
            assert mem.page_size == page, name
            assert mem.size == size, name
            assert mem.saddr == (saddr if saddr is not None else 0x50), name

    @pytest.mark.asyncio
    async def test_summon_without_saddr_fails(self):
        bus, _ = make_bus()
        with pytest.raises(ValueError):
            await bus.child_summon("24lc64")

    @pytest.mark.asyncio
    async def test_summon_unknown_name_fails(self):
        bus, _ = make_bus()
        with pytest.raises(NoMatch):
            await bus.child_summon("not-a-part")

    @pytest.mark.asyncio
    async def test_summoned_node_is_usable(self):
        device = FakeDevice(saddr=0x50, addr_bytes=2, size=8192)
        bus, adapter = make_bus(device)
        mem = await bus.child_summon("24lc64(saddr=0x50)")
        await mem.mem_write(0x40, b"hello")
        assert await mem.mem_read(0x40, 5) == b"hello"


class TestRegisterEndianness:
    class Bulk(RegisterFromBulk, Batcher, Node):
        ops = MemoryInterface.BULK_OPS

        def __init__(self, storage):
            Batcher.__init__(self)
            Node.__init__(self, "bulk")
            self.storage = storage

        async def flush_ops(self, batch):
            for op, future in batch:
                if isinstance(op, ReadBlob):
                    future.set_result(
                        bytes(self.storage[op.addr:op.addr + op.size]))
                else:
                    self.storage[op.addr:op.addr + len(op.data)] = op.data
                    future.set_result(None)

    @pytest.mark.asyncio
    async def test_default_is_little_endian(self):
        bus = self.Bulk(bytearray(b"\x01\x02\x03\x04"))
        assert await bus.read32(0) == 0x04030201

    @pytest.mark.asyncio
    async def test_big_endian_read(self):
        bus = self.Bulk(bytearray(b"\x01\x02\x03\x04"))
        bus.register_endianness = "big"
        assert await bus.read32(0) == 0x01020304
        assert await bus.read16(0) == 0x0102

    @pytest.mark.asyncio
    async def test_little_endian_write(self):
        bus = self.Bulk(bytearray(4))
        await bus.write32(0, 0x01020304)
        assert bytes(bus.storage) == b"\x04\x03\x02\x01"

    @pytest.mark.asyncio
    async def test_big_endian_write(self):
        bus = self.Bulk(bytearray(4))
        bus.register_endianness = "big"
        await bus.write32(0, 0x01020304)
        assert bytes(bus.storage) == b"\x01\x02\x03\x04"


class TestTarget:
    async def make_tree(self, **kwargs):
        device = FakeDevice(saddr=0x50, addr_bytes=2,
                            size=kwargs.get("size", 0x2000))
        bus, adapter = make_bus(device)
        root = Node("root")
        mem = I2cEeprom(bus, "eeprom", saddr=0x50, addr_bytes=2,
                        page_size=32, **kwargs)
        root.child_add(mem)
        await mem.ensure_started()
        return root, mem, adapter, device

    @pytest.mark.asyncio
    async def test_discovery_spawns_target(self):
        root, mem, _, _ = await self.make_tree()
        spawned = await TargetDiscovery().run(root)
        assert len(spawned) == 1
        target = spawned[0]
        assert isinstance(target, I2cMemTarget)
        assert target.parent is root
        assert mem in target.claimed_components()

    @pytest.mark.asyncio
    async def test_unstarted_component_is_declined(self):
        bus, _ = make_bus()
        root = Node("root")
        root.child_add(I2cEeprom(bus, "eeprom", saddr=0x50))
        assert await TargetDiscovery().run(root) == []

    @pytest.mark.asyncio
    async def test_discovery_no_duplicate(self):
        root, _, _, _ = await self.make_tree()
        disc = TargetDiscovery()
        await disc.run(root)
        assert await disc.run(root) == []
        assert len(root.children_of_class(Target)) == 1

    @pytest.mark.asyncio
    async def test_region_geometry(self):
        root, mem, _, _ = await self.make_tree()
        target = (await TargetDiscovery().run(root))[0]
        loadable = target.children_of_class(Loadable)[0]
        region = loadable.children_of_class(I2cMemRegion)[0]
        assert region.address == 0
        assert region.size == mem.size
        assert region.write_page_size == 32
        assert region.is_blank is False

    @pytest.mark.asyncio
    async def test_loadable_write_and_readback(self):
        root, _, adapter, device = await self.make_tree()
        target = (await TargetDiscovery().run(root))[0]
        loadable = target.children_of_class(Loadable)[0]

        m = MemoryMap()
        m.append(0x40, bytes(range(64)))
        await loadable.write(m)

        assert bytes(device.storage[0x40:0x80]) == bytes(range(64))
        # Two 32-byte pages, each gated by its own WaitAck.
        writes = [tx for tx in adapter.transactions
                  if any(isinstance(i, i2c.Transfer) and not i.size_r
                         for i in tx.items)]
        assert len(writes) == 2
        assert [tx.items[1].data_w[:2] for tx in writes] == [
            b"\x00\x40", b"\x00\x60"]
        assert all(isinstance(tx.items[0], i2c.WaitAck) for tx in writes)

        readback = await loadable.read(0x40, 0x80)
        assert readback.read(0x40, 64) == bytes(range(64))

    @pytest.mark.asyncio
    async def test_loadable_verify(self):
        root, _, _, _ = await self.make_tree()
        target = (await TargetDiscovery().run(root))[0]
        loadable = target.children_of_class(Loadable)[0]

        m = MemoryMap()
        m.append(0x100, b"payload")
        await loadable.write(m)
        assert await loadable.verify(m) is True

        bad = MemoryMap()
        bad.append(0x100, b"PAYLOAD")
        assert await loadable.verify(bad) is False

    @pytest.mark.asyncio
    async def test_erase_all_is_a_noop(self):
        """No Flash region: erase_all touches nothing."""
        root, _, adapter, _ = await self.make_tree()
        target = (await TargetDiscovery().run(root))[0]
        loadable = target.children_of_class(Loadable)[0]
        await loadable.erase_all()
        assert adapter.transactions == []


class TestWriteBlobOp:
    @pytest.mark.asyncio
    async def test_ops_declared_bulk_only(self):
        assert I2cMem.ops == MemoryInterface.BULK_OPS
        assert ReadBlob in I2cMem.ops
        assert WriteBlob in I2cMem.ops
