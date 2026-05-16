"""Tests for `PicobootSpiInterface` — the on-target SPI passthrough
stub driven through PICOBOOT.

The mock transport interprets each PICOBOOT EXEC as "run the SPI
stub": parse the patched cmd-array address out of the stub bytes,
walk the command entries, simulate the CS pin state, and for each
transfer build a synthetic miso response (here: a configurable per-
test function). That keeps the tests focused on the host-side
framing (cmd-array layout, tx/rx pointer wiring, end marker, stub
patch) without depending on actual on-target execution.
"""

import asyncio
import struct

import pytest

from acrobe.component.raspberry.picoboot import Picoboot, PicobootPuppet
from acrobe.component.raspberry.spi import (
    PicobootSpiInterface, SPI_TRANSACT_STUB, PLACEHOLDER,
    CMD_CS_LOW, CMD_CS_HIGH,
)
from acrobe.protocol import spi
from acrobe.target.region import Ram
from acrobe.target.region import Ram


class MockPicobootTransport:
    """Models RP2040 SRAM as a bytearray; EXEC walks the SPI stub's
    cmd array and runs a host-supplied miso provider."""

    SRAM_BASE = 0x20000000

    def __init__(self):
        self.ram = bytearray(0x42000)
        self.exit_xip_calls = 0
        self.exec_log: list[int] = []
        # Function (cs_state: bool, tx_bytes: bytes) -> miso_bytes.
        # cs_state True = CS asserted (driven LOW), False = deasserted.
        self.on_transfer = lambda cs, tx: bytes(len(tx))
        # Recorded SPI transactions: list of dicts {cs, transfers}.
        self.transactions: list[dict] = []

    def __off(self, addr):
        off = addr - self.SRAM_BASE
        assert 0 <= off < len(self.ram), f"addr 0x{addr:08x} out of mock SRAM"
        return off

    async def read(self, addr, size):
        off = self.__off(addr)
        return bytes(self.ram[off:off + size])

    async def write(self, addr, data):
        off = self.__off(addr)
        self.ram[off:off + len(data)] = bytes(data)

    async def exec(self, pc):
        self.exec_log.append(pc)
        # Locate stub bytes (we'll search RAM for the unique 12-byte
        # prefix of the compiled stub — much simpler than chasing the
        # entry point).
        stub_marker = SPI_TRANSACT_STUB[:12]
        stub_off = self.ram.find(stub_marker)
        assert stub_off >= 0, "stub bytes not in RAM"
        # Extract the patched cmd-array address from the marker slot.
        ph_off = stub_off + SPI_TRANSACT_STUB.index(
            PLACEHOLDER.to_bytes(4, "little"))
        cmd_addr = struct.unpack(
            "<I", self.ram[ph_off:ph_off + 4])[0]
        cur = self.__off(cmd_addr)
        cs_state = False  # default after reset; tests should always
        # assert CS before transferring.
        txn: dict = {"cs": False, "transfers": []}
        while True:
            tx_ptr, rx_ptr, size = struct.unpack(
                "<III", self.ram[cur:cur + 12])
            if size == 0:
                break
            if size & 0x80000000:
                cs_state = (size & 1) == 0  # bit 0 set = CS high
                txn["cs"] = cs_state
                cur += 12
                continue
            tx_data = (bytes(self.ram[self.__off(tx_ptr):
                                      self.__off(tx_ptr) + size])
                       if tx_ptr else bytes(size))
            miso = self.on_transfer(cs_state, tx_data)
            if rx_ptr:
                self.__write_to(rx_ptr, miso[:size].ljust(size, b"\x00"))
            txn["transfers"].append({
                "cs": cs_state, "tx": tx_data, "miso": miso[:size]})
            cur += 12
        self.transactions.append(txn)

    def __write_to(self, addr, data):
        off = self.__off(addr)
        self.ram[off:off + len(data)] = data

    async def exit_xip(self):
        self.exit_xip_calls += 1


def make_iface():
    transport = MockPicobootTransport()
    picoboot = Picoboot(transport)
    ram = Ram("sram", 0x20000000, 0x42000 - 0x1000)
    puppet = PicobootPuppet("puppet", ram, picoboot)
    iface = PicobootSpiInterface(picoboot, puppet)
    return iface, transport


class TestStartup:
    @pytest.mark.asyncio
    async def test_start_calls_exit_xip(self):
        iface, transport = make_iface()
        await iface.start()
        assert transport.exit_xip_calls == 1

    @pytest.mark.asyncio
    async def test_adds_cs0_target(self):
        iface, _ = make_iface()
        targets = iface.children_of_class(spi.Target)
        assert len(targets) == 1
        assert targets[0].name == "cs0"
        assert targets[0].cs == 0


class TestSingleTransfer:
    @pytest.mark.asyncio
    async def test_write_only(self):
        iface, transport = make_iface()
        cs0 = iface.children_of_class(spi.Target)[0]
        await cs0.transaction(
            spi.Shift(b"\x9f\x00\x00\x00", read_miso=False))
        assert len(transport.transactions) == 1
        txn = transport.transactions[0]
        # CS was driven low for the shift, then high at deassert.
        assert txn["cs"] is False  # last state: CS deasserted
        assert len(txn["transfers"]) == 1
        t = txn["transfers"][0]
        assert t["cs"] is True  # asserted during transfer
        assert t["tx"] == b"\x9f\x00\x00\x00"

    @pytest.mark.asyncio
    async def test_read_distributes_miso(self):
        iface, transport = make_iface()
        # SPI flash JEDEC ID-style response.
        transport.on_transfer = lambda cs, tx: bytes([0x00] + [0xEF, 0x40, 0x16])
        cs0 = iface.children_of_class(spi.Target)[0]
        shift_cmd = spi.Shift(b"\x9f", read_miso=True)
        shift_data = spi.Shift(3, read_miso=True)  # 3 zeros, read 3 bytes
        await cs0.transaction(shift_cmd, shift_data)
        # The mock returns the same response for both transfers; the
        # interesting bit is that miso bytes were sliced per Shift.
        assert shift_cmd.miso is not None
        assert shift_data.miso is not None
        assert len(shift_cmd.miso) == 1
        assert len(shift_data.miso) == 3


class TestCmdLayout:
    @pytest.mark.asyncio
    async def test_cs_entries_are_marked(self):
        """CS entries have the high bit set in `size`; bit 0
        encodes high vs low."""
        iface, transport = make_iface()
        cs0 = iface.children_of_class(spi.Target)[0]
        await cs0.transaction(spi.Shift(b"\x55", read_miso=False))
        # The stub saw CS asserted then deasserted around the shift.
        assert transport.transactions[0]["transfers"][0]["cs"] is True
        # After deassert, the final CS state should be high (False
        # in our mock — we track "asserted" boolean).
        assert transport.transactions[0]["cs"] is False

    @pytest.mark.asyncio
    async def test_stub_uploaded_once(self):
        """Multiple transactions reuse the same stub upload."""
        iface, transport = make_iface()
        cs0 = iface.children_of_class(spi.Target)[0]
        await cs0.transaction(spi.Shift(b"\x01", read_miso=False))
        await cs0.transaction(spi.Shift(b"\x02", read_miso=False))
        # Two EXECs (one per transaction).
        assert len(transport.exec_log) == 2
        # Stub appears exactly once in RAM (search for first 12
        # bytes — unique signature).
        marker = SPI_TRANSACT_STUB[:12]
        positions = []
        start = 0
        while True:
            p = transport.ram.find(marker, start)
            if p < 0:
                break
            positions.append(p)
            start = p + 1
        assert len(positions) == 1


class TestStubBytes:
    def test_marker_is_unique(self):
        marker = PLACEHOLDER.to_bytes(4, "little")
        assert SPI_TRANSACT_STUB.count(marker) == 1

    def test_size_matches_compiled(self):
        # crobe rp2040.c → 124 bytes, ARMv6-M Thumb.
        assert len(SPI_TRANSACT_STUB) == 124

    def test_constants(self):
        assert CMD_CS_LOW == 0x80000000
        assert CMD_CS_HIGH == 0x80000001
