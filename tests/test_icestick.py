"""iCEstick board support: MPSSE SPI master, sync-bitbang shifter,
iCE40 slave-serial loading, bitstream format and board adapter."""

import asyncio
import logging

import pytest

import acrobe.adapter  # noqa: F401 — fires adapter registrations
import acrobe.component.lattice.formats  # noqa: F401
from acrobe.adapter.ftdi import mpsse_cmd
from acrobe.adapter.ftdi.mpsse import MpsseEngine
from acrobe.adapter.ftdi.spi import SpiMpsse
from acrobe.adapter.ftdi.spi_bitbang import SpiBitbang
from acrobe.adapter.icestick import IceStickAdapter, IceStickFlashSpi
from acrobe.adapter.model import adapter_db
from acrobe.component.lattice.formats import (
    ICE40_COMMENT_END, ICE40_COMMENT_START, ICE40_SYNC, Ice40Bit,
    LatticePayload,
)
from acrobe.component.lattice.ice40 import Ice40SlaveSerial
from acrobe.db import NoMatch
from acrobe.protocol import spi
from acrobe.vfs import FsRoot

_logger = logging.getLogger("test.icestick")


def _resolved(value):
    f = asyncio.get_running_loop().create_future()
    f.set_result(value)
    return f


class MockTransport:
    """MPSSE transport mock: records writes, serves queued responses."""

    def __init__(self):
        self.writes = []
        self.responses = []
        self.closed = False

    def queue_response(self, data: bytes):
        self.responses.append(data)

    def write(self, data: bytes):
        self.writes.append(bytes(data))
        return _resolved(None)

    def read(self, byte_count: int):
        if self.responses:
            rsp = self.responses.pop(0)
            assert len(rsp) == byte_count, \
                f"queued response is {len(rsp)}B, engine expects {byte_count}B"
        else:
            rsp = bytes(byte_count)
        return _resolved(rsp)

    async def close(self):
        self.closed = True


class MockBitbangTransport:
    """Sync-bitbang transport mock: one sampled byte per byte written."""

    def __init__(self):
        self.writes = []
        self.baudrates = []
        self.samples = []
        self.closed = False

    def queue_samples(self, data: bytes):
        self.samples.append(data)

    def write(self, data: bytes):
        self.writes.append(bytes(data))
        return _resolved(None)

    def read(self, byte_count: int, timeout=None):
        if self.samples:
            rsp = self.samples.pop(0)
            assert len(rsp) == byte_count
        else:
            rsp = bytes(byte_count)
        return _resolved(rsp)

    async def set_baudrate(self, baudrate):
        self.baudrates.append(baudrate)
        return baudrate

    async def close(self):
        self.closed = True


# --- SpiMpsse ---

ICESTICK_OE = 0x93   # SCK, MOSI, CS, CRESET
ICESTICK_CS_HIGH = 0x10


def _make_spi(transport, **kwargs):
    engine = MpsseEngine(transport, _logger)
    kwargs.setdefault("cs_pin", 4)
    return SpiMpsse(engine, **kwargs)


class TestSpiMpsse:
    @pytest.mark.asyncio
    async def test_start_sequence(self):
        transport = MockTransport()
        iface = _make_spi(transport, gpio_oe=0x80, gpio_val=0x00)
        await iface.start()

        assert transport.writes == [bytes([
            mpsse_cmd.CLK_DIV5_DISABLE,
            mpsse_cmd.CLK_DIV, 29, 0,          # 60 MHz / (2*30) = 1 MHz
            mpsse_cmd.THREE_PHASE_DISABLE,
            mpsse_cmd.ADAPTIVE_DISABLE,
            mpsse_cmd.LOOPBACK_DISABLE,
            mpsse_cmd.SET_BITS_LOW, ICESTICK_CS_HIGH, ICESTICK_OE,
            mpsse_cmd.GET_BITS_LOW,            # engine sync byte
            mpsse_cmd.SEND_IMMEDIATE,
        ])]

    @pytest.mark.asyncio
    async def test_freq_divisor(self):
        transport = MockTransport()
        iface = _make_spi(transport)
        await iface.start()
        transport.writes.clear()

        assert iface.freq_cap("user", 6e6) == 6e6
        await asyncio.sleep(0)
        await iface.gpio_get()

        assert transport.writes[0][:5] == bytes([
            mpsse_cmd.CLK_DIV5_DISABLE, mpsse_cmd.CLK_DIV, 4, 0,
            mpsse_cmd.GET_BITS_LOW])

    @pytest.mark.asyncio
    async def test_write_only_transaction(self):
        transport = MockTransport()
        iface = _make_spi(transport, gpio_oe=0x80)
        await iface.start()
        transport.writes.clear()

        target = iface.child_lookup("cs0")
        await target.transaction(spi.Shift(b"\x9f\x00", read_miso=False))

        assert transport.writes == [bytes([
            mpsse_cmd.SET_BITS_LOW, 0x00, ICESTICK_OE,        # CS asserted
            mpsse_cmd.WRITE | mpsse_cmd.WRITE_NEG, 1, 0, 0x9f, 0x00,
            mpsse_cmd.SET_BITS_LOW, ICESTICK_CS_HIGH, ICESTICK_OE,
            mpsse_cmd.GET_BITS_LOW,
            mpsse_cmd.SEND_IMMEDIATE,
        ])]

    @pytest.mark.asyncio
    async def test_read_transaction_reassembles_miso(self):
        transport = MockTransport()
        iface = _make_spi(transport)
        await iface.start()
        transport.writes.clear()
        transport.queue_response(bytes([0xef, 0x40, 0x18]))

        target = iface.child_lookup("cs0")
        read = spi.Shift(3, read_miso=True)
        await target.transaction(spi.Shift(b"\x9f", read_miso=False), read)

        assert read.miso == bytes([0xef, 0x40, 0x18])
        cmd = transport.writes[0]
        assert bytes([mpsse_cmd.WRITE | mpsse_cmd.WRITE_NEG | mpsse_cmd.READ,
                      2, 0, 0, 0, 0]) in cmd

    @pytest.mark.asyncio
    async def test_mode3_moves_clock_before_select(self):
        transport = MockTransport()
        iface = _make_spi(transport)
        await iface.start()
        transport.writes.clear()

        iface.post(spi.Cs(0, mode=3))
        await iface.post(spi.Shift(b"\x55", read_miso=False))

        assert transport.writes == [bytes([
            mpsse_cmd.SET_BITS_LOW, ICESTICK_CS_HIGH | 0x01, 0x13,
            mpsse_cmd.SET_BITS_LOW, 0x01, 0x13,
            mpsse_cmd.WRITE | mpsse_cmd.WRITE_NEG, 0, 0, 0x55,
            mpsse_cmd.GET_BITS_LOW,
            mpsse_cmd.SEND_IMMEDIATE,
        ])]

    @pytest.mark.asyncio
    async def test_gpio_access(self):
        transport = MockTransport()
        iface = _make_spi(transport, gpio_oe=0x80, gpio_val=0x00)
        await iface.start()
        transport.writes.clear()
        transport.queue_response(bytes([0x00]))
        transport.queue_response(bytes([0x00]))
        transport.queue_response(bytes([0x40]))

        await iface.gpio_set(0x80, 0x80)
        assert iface.gpio_value & 0x80
        await iface.gpio_oe_set(0xff, 0x80)
        assert iface.gpio_oe == 0x80
        assert await iface.gpio_get() == 0x40

        assert transport.writes[0] == bytes([
            mpsse_cmd.SET_BITS_LOW, 0x90, ICESTICK_OE,
            mpsse_cmd.GET_BITS_LOW, mpsse_cmd.SEND_IMMEDIATE])
        assert transport.writes[1] == bytes([
            mpsse_cmd.SET_BITS_LOW, 0x90, 0x80,
            mpsse_cmd.GET_BITS_LOW, mpsse_cmd.SEND_IMMEDIATE])

    @pytest.mark.asyncio
    async def test_unknown_op_rejected(self):
        transport = MockTransport()
        iface = _make_spi(transport)
        await iface.start()
        with pytest.raises(TypeError):
            await iface.post(object())


# --- SpiBitbang ---

BB_PINS = dict(sck=0, mosi=2, csn=4)
BB_OUTPUTS = {"creset": 7}
BB_INPUTS = {"cdone": 6}


def _make_shifter(transport):
    return SpiBitbang(transport, outputs=BB_OUTPUTS, inputs=BB_INPUTS,
                      **BB_PINS)


class TestSpiBitbang:
    def test_oe_mask(self):
        assert SpiBitbang.oe_mask(outputs=BB_OUTPUTS, **BB_PINS) == 0x95

    @pytest.mark.asyncio
    async def test_shift_stream(self):
        transport = MockBitbangTransport()
        shifter = _make_shifter(transport)
        await shifter.shift(b"\xa5")

        # 0xa5 = 1010_0101, MSB first; data on bit 2, clock on bit 0.
        expected = bytearray()
        for bit in (1, 0, 1, 0, 0, 1, 0, 1):
            low = 0x04 if bit else 0x00
            expected.append(low)
            expected.append(low | 0x01)
        expected.append(0x04)   # clock returns low, last bit still driven
        assert transport.writes == [bytes(expected)]

    @pytest.mark.asyncio
    async def test_pin_levels_persist_across_shift(self):
        transport = MockBitbangTransport()
        shifter = _make_shifter(transport)
        await shifter.output_set("creset", 1)
        await shifter.cs_set(True)
        await shifter.shift(b"\x80")
        await shifter.cs_set(False)

        assert transport.writes[0] == bytes([0x80])
        assert transport.writes[1] == bytes([0x80])
        # CRESET stays high all along the shift, CS stays low.
        assert all(b & 0x80 for b in transport.writes[2])
        assert not any(b & 0x10 for b in transport.writes[2])
        assert transport.writes[3] == bytes([0x90])

    @pytest.mark.asyncio
    async def test_clocks_sample_rising_edges(self):
        transport = MockBitbangTransport()
        # 3 cycles -> 7 bytes; CDONE (bit 6) high on the second rising edge.
        transport.queue_samples(bytes([0, 0, 0, 0x40, 0, 0, 0]))
        shifter = _make_shifter(transport)

        assert await shifter.clocks(3, sample="cdone") == [False, True, False]
        assert transport.writes == [bytes([0, 1, 0, 1, 0, 1, 0])]

    @pytest.mark.asyncio
    async def test_clocks_without_sample(self):
        transport = MockBitbangTransport()
        shifter = _make_shifter(transport)
        assert await shifter.clocks(2) is None

    @pytest.mark.asyncio
    async def test_input_get(self):
        transport = MockBitbangTransport()
        transport.queue_samples(bytes([0x40]))
        shifter = _make_shifter(transport)
        assert await shifter.input_get("cdone") is True

    @pytest.mark.asyncio
    async def test_undeclared_pin(self):
        shifter = _make_shifter(MockBitbangTransport())
        with pytest.raises(KeyError):
            await shifter.output_set("nreset", 1)

    @pytest.mark.asyncio
    async def test_miso_read_unsupported(self):
        shifter = _make_shifter(MockBitbangTransport())
        with pytest.raises(NotImplementedError):
            await shifter.shift(b"\x00", read_miso=True)

    @pytest.mark.asyncio
    async def test_bit_freq_cap_derates(self):
        transport = MockBitbangTransport()
        shifter = _make_shifter(transport)
        await shifter.bit_freq_cap(15e6)
        assert transport.baudrates == [2 * 15e6 / SpiBitbang.CLOCK_UNCERTAINTY]

    @pytest.mark.asyncio
    async def test_chunking(self):
        transport = MockBitbangTransport()
        shifter = _make_shifter(transport)
        await shifter.shift(bytes(SpiBitbang.PAYLOAD_CHUNK + 1))
        assert len(transport.writes) == 2
        assert len(transport.writes[0]) == 16 * SpiBitbang.PAYLOAD_CHUNK + 1
        assert len(transport.writes[1]) == 17


# --- Ice40SlaveSerial ---

class MockShifter:
    """Records the configuration sequence; CDONE rises after a
    configurable number of polling clocks."""

    def __init__(self, cdone_after=8):
        self.log = []
        self.cdone_after = cdone_after
        self.polled = 0

    async def bit_freq_cap(self, freq):
        self.log.append(("freq", freq))

    async def output_set(self, name, level):
        self.log.append(("out", name, level))

    async def cs_set(self, asserted):
        self.log.append(("cs", asserted))

    async def wait(self, seconds):
        self.log.append(("wait", seconds))

    async def shift(self, data):
        self.log.append(("shift", len(data)))

    async def clocks(self, count, sample=None):
        self.log.append(("clocks", count, sample))
        if sample is None:
            return None
        self.polled += count
        done = self.cdone_after is not None and self.polled >= self.cdone_after
        return [done] * count

    async def input_get(self, name):
        self.log.append(("in", name))
        return True


class TestIce40SlaveSerial:
    @pytest.mark.asyncio
    async def test_load_sequence(self):
        shifter = MockShifter(cdone_after=16)
        fpga = Ice40SlaveSerial(shifter)
        await fpga.load(LatticePayload("bitstream", bytes(64)))

        assert shifter.log == [
            ("freq", Ice40SlaveSerial.MAX_FREQ),
            ("out", "creset", 0),
            ("cs", True),
            ("out", "creset", 1),
            ("wait", Ice40SlaveSerial.RESET_SETTLE_S),
            ("cs", False),
            ("clocks", Ice40SlaveSerial.LEAD_CLOCKS, None),
            ("cs", True),
            ("shift", 64),
            ("cs", False),
            ("clocks", 8, Ice40SlaveSerial.CDONE),
            ("clocks", 8, Ice40SlaveSerial.CDONE),
            ("clocks", Ice40SlaveSerial.TRAILING_CLOCKS, None),
        ]

    @pytest.mark.asyncio
    async def test_cdone_timeout(self):
        shifter = MockShifter(cdone_after=None)
        fpga = Ice40SlaveSerial(shifter)
        with pytest.raises(RuntimeError, match="CDONE"):
            await fpga.load(LatticePayload("bitstream", bytes(8)))

        polls = [e for e in shifter.log if e[0] == "clocks" and e[2]]
        assert sum(e[1] for e in polls) == Ice40SlaveSerial.CDONE_CLOCKS
        # The device never gets the trailing clocks of a good load.
        assert shifter.log[-1][0] == "clocks"
        assert shifter.log[-1][2] == Ice40SlaveSerial.CDONE

    @pytest.mark.asyncio
    async def test_erase_holds_reset(self):
        shifter = MockShifter()
        fpga = Ice40SlaveSerial(shifter)
        await fpga.erase()
        assert shifter.log == [
            ("out", "creset", 0),
            ("wait", Ice40SlaveSerial.RESET_SETTLE_S),
        ]

    @pytest.mark.asyncio
    async def test_is_configured(self):
        shifter = MockShifter()
        fpga = Ice40SlaveSerial(shifter)
        assert await fpga.is_configured() is True
        assert shifter.log == [("in", Ice40SlaveSerial.CDONE)]

    @pytest.mark.asyncio
    async def test_programmed_through_fpga_target(self):
        from acrobe.target import Loadable
        from acrobe.target.fpga import FpgaTarget

        shifter = MockShifter(cdone_after=8)
        target = FpgaTarget(Ice40SlaveSerial(shifter))
        loadable = target.children_of_class(Loadable)[0]
        await loadable.write(LatticePayload("bitstream", bytes(4)))

        assert ("shift", 4) in shifter.log


# --- Bitstream format ---

def _ice40_blob(comment: bytes | None, body: bytes):
    head = b""
    if comment is not None:
        head = ICE40_COMMENT_START + comment + ICE40_COMMENT_END
    return head + ICE40_SYNC + body


class TestIce40Format:
    @pytest.mark.asyncio
    async def test_bare_sync(self, tmp_path):
        blob = _ice40_blob(None, bytes(range(32)))
        (tmp_path / "top.bin").write_bytes(blob)
        root = FsRoot(str(tmp_path))
        await root.start_tree()

        leaf = await root.child_summon("top.bin")
        assert leaf.metadata["sync_offset"] == 0
        view = await root.child_summon("top.bin", "bitstream")
        assert await view.read(0, view.size) == blob

    @pytest.mark.asyncio
    async def test_comment_block(self, tmp_path):
        comment = b"Part: iCE40HX1K-TQ144\x00free form text\x00"
        blob = _ice40_blob(comment, bytes(16))
        (tmp_path / "top.bin").write_bytes(blob)
        root = FsRoot(str(tmp_path))
        await root.start_tree()

        leaf = await root.child_summon("top.bin")
        assert leaf.metadata["Part"] == "iCE40HX1K-TQ144"
        assert leaf.metadata["comment"] == "free form text"
        assert leaf.metadata["sync_offset"] == len(blob) - 16 - len(ICE40_SYNC)

        view = await root.child_summon("top.bin", "bitstream")
        assert (await view.read(0, 4)) == ICE40_SYNC
        assert view.size == len(ICE40_SYNC) + 16

    @pytest.mark.asyncio
    async def test_rejects_foreign_blob(self, tmp_path):
        (tmp_path / "junk.bin").write_bytes(bytes(range(256)) * 4)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("junk.bin")
        assert leaf.child_lookup("bitstream") is None

    @pytest.mark.asyncio
    async def test_sync_behind_unknown_prefix_declines(self):
        source = LatticePayload("blob", b"\x11\x22" + ICE40_SYNC + bytes(8))
        parser = Ice40Bit("ice40_bit", source)
        with pytest.raises(NoMatch):
            await parser.start()


# --- Board adapter ---

class _FakeDescriptor:
    def __init__(self, vendor_id=0x0403, product_id=0x6010,
                 manufacturer="Lattice",
                 product="Lattice FTUSB Interface Cable"):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.manufacturer = manufacturer
        self.product = product
        self.opened = 0

    def open(self):
        self.opened += 1
        return _FakeDevice()


class _FakeDevice:
    class _Handle:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def __init__(self):
        self.handle = self._Handle()


def _icestick_info():
    for info, classes in adapter_db.registry.items():
        if info.name == "icestick":
            return info, classes
    raise AssertionError("icestick not registered")


class TestIceStickRegistration:
    def test_registered_for_lattice_strings(self):
        info, classes = _icestick_info()
        assert IceStickAdapter in classes
        assert info.matches(_FakeDescriptor())

    def test_declines_other_ftdi_boards(self):
        info, _ = _icestick_info()
        assert not info.matches(_FakeDescriptor(manufacturer="Digilent"))
        assert not info.matches(_FakeDescriptor(product="Dual RS232-HS"))
        assert not info.matches(_FakeDescriptor(product_id=0x6014))

    def test_child_hints(self):
        adapter = IceStickAdapter("icestick", _icestick_info()[0],
                                  _FakeDescriptor())
        assert adapter.child_hints() == ["spi", "ice40", "jtag-io", "spi-io"]


class TestIceStickChannels:
    @pytest.fixture
    def adapter(self, monkeypatch):
        import acrobe.adapter.icestick as module

        transports = []

        class _Transport:
            @classmethod
            async def from_device(cls, device, interface_index=0):
                transport = MockTransport()
                transport.interface_index = interface_index
                transports.append(transport)
                return transport

        shifters = []

        class _Shifter:
            @classmethod
            async def open(cls, device, **kwargs):
                shifter = MockShifter()
                shifter.kwargs = kwargs
                shifter.closed = False

                async def close():
                    shifter.closed = True

                shifter.close = close
                shifters.append(shifter)
                return shifter

        monkeypatch.setattr(module, "FtdiTransport", _Transport)
        monkeypatch.setattr(module, "SpiBitbang", _Shifter)

        adapter = IceStickAdapter("icestick", _icestick_info()[0],
                                  _FakeDescriptor())
        adapter.transports = transports
        adapter.shifters = shifters
        return adapter

    @pytest.mark.asyncio
    async def test_flash_spi_holds_creset(self, adapter):
        iface = await adapter.child_spawn("spi")
        assert isinstance(iface, IceStickFlashSpi)
        # CRESET (bit 7) driven low: the FPGA cannot boot and take
        # over the bus.
        assert iface.gpio_oe == ICESTICK_OE
        assert not (iface.gpio_value & 0x80)

        await iface.start()
        await iface.stop()
        assert iface.gpio_value & 0x80
        assert iface.gpio_oe == 0x80

    @pytest.mark.asyncio
    async def test_port_a_children_are_exclusive(self, adapter):
        iface = await adapter.child_spawn("spi")
        await iface.start()
        fpga = await adapter.child_spawn("ice40")

        assert isinstance(fpga, Ice40SlaveSerial)
        assert adapter.transports[0].closed
        assert fpga.shifter.kwargs["mosi"] == IceStickAdapter.A_MISO
        assert fpga.shifter.kwargs["interface_index"] == \
            IceStickAdapter.CHANNEL_A

        await adapter.child_spawn("spi")
        assert adapter.shifters[0].closed

    @pytest.mark.asyncio
    async def test_port_b_children_are_exclusive_and_independent(
            self, adapter):
        flash = await adapter.child_spawn("spi")
        jtag = await adapter.child_spawn("jtag-io")
        assert jtag.name == "jtag-io"
        assert adapter.transports[1].interface_index == \
            IceStickAdapter.CHANNEL_B
        # Port A untouched by a port B summon.
        assert not adapter.transports[0].closed

        io_spi = await adapter.child_spawn("spi-io")
        assert io_spi.name == "spi-io"
        assert adapter.transports[1].closed
        assert not adapter.transports[0].closed
        assert flash is not None

    @pytest.mark.asyncio
    async def test_unknown_child(self, adapter):
        with pytest.raises(NoMatch):
            await adapter.child_spawn("uart")

    @pytest.mark.asyncio
    async def test_close_releases_everything(self, adapter):
        await (await adapter.child_spawn("spi")).start()
        await adapter.child_spawn("jtag-io")
        await adapter.close()

        assert all(t.closed for t in adapter.transports)
