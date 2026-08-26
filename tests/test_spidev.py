"""Linux spidev adapter (`acrobe.adapter.linux.spidev`).

Everything the interface does reaches the kernel through
`CharDevice.call`, so a fake that decodes the request number and
follows the transfer array's pointers exercises the whole lowering
without hardware.
"""

import asyncio
import ctypes
import errno
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux ioctl ABI")

from acrobe.adapter.linux.char_device import CharDevice  # noqa: E402
from acrobe.adapter.linux.spidev import (  # noqa: E402
    MAX_TRANSFERS, SPI_NO_CS, SpiIocTransfer, SpidevAdapter,
    SpidevEnumerator, SpidevInterface,
)
from acrobe.bitstring import BitString  # noqa: E402
from acrobe.node import Node  # noqa: E402
from acrobe.protocol import spi  # noqa: E402


def apply_option(node, key, value):
    """Apply an option the way `child_summon` does.

    Node's MRO walk is private, and calling `option_set` directly would
    reach only the interface's own handler -- never FreqCapper's
    ``fmax=``.
    """
    Node._Node__option_set(node, key, value)


class FakeSpiDevice(CharDevice):
    """spidev far enough to drive the lowering.

    Records one entry per SPI_IOC_MESSAGE as
    ``(mode_at_the_time, [transfer, ...])`` where each transfer is a
    dict of the fields the lowering is responsible for.
    """

    def __init__(self, path="/dev/spidev0.0", *, mode=0,
                 max_speed_hz=50_000_000, miso=b"", fail_no_cs=False):
        super().__init__(path)
        self.mode = mode
        self.max_speed_hz = max_speed_hz
        self.miso = miso
        self.fail_no_cs = fail_no_cs
        self.messages = []
        self.mode_writes = []
        self.raise_on_message = None
        self.__opened = False
        # MISO is one continuous stream across messages, as a real
        # device's would be.
        self.__served = 0

    async def open(self):
        self.__opened = True

    async def close(self):
        self.__opened = False

    @property
    def opened(self):
        return self.__opened

    def call(self, request, arg, mutate=True):
        nr = request & 0xFF
        size = (request >> 16) & 0x3FFF
        direction = request >> 30
        if nr == 0:
            return self.__message(arg, size // ctypes.sizeof(SpiIocTransfer))
        if nr == 5 and direction == 2:
            arg.value = self.mode
            return 0
        if nr == 5 and direction == 1:
            if self.fail_no_cs and arg.value & SPI_NO_CS:
                raise OSError(errno.EINVAL, "Invalid argument")
            self.mode_writes.append(arg.value)
            self.mode = arg.value
            return 0
        if nr == 4 and direction == 2:
            arg.value = self.max_speed_hz
            return 0
        raise AssertionError(f"unexpected ioctl 0x{request:08x}")

    def __message(self, transfers, count):
        if self.raise_on_message is not None:
            raise self.raise_on_message
        entry = []
        for index in range(count):
            xfer = transfers[index]
            entry.append(dict(
                len=xfer.len,
                speed_hz=xfer.speed_hz,
                bits_per_word=xfer.bits_per_word,
                cs_change=xfer.cs_change,
                reads=bool(xfer.rx_buf),
                mosi=ctypes.string_at(xfer.tx_buf, xfer.len),
            ))
            if xfer.rx_buf:
                chunk = self.__miso_slice(self.__served, xfer.len)
                ctypes.memmove(xfer.rx_buf, chunk, len(chunk))
                self.__served += xfer.len
        self.messages.append((self.mode, entry))
        return 0

    def __miso_slice(self, offset, length):
        if not self.miso:
            return bytes(length)
        # Repeat the scripted pattern so a read of any size is served.
        repeats = (offset + length) // len(self.miso) + 2
        return (self.miso * repeats)[offset:offset + length]

    @property
    def transfers(self):
        """Every transfer, flattened across messages."""
        return [t for _mode, entry in self.messages for t in entry]


@pytest.fixture
def bufsiz(monkeypatch):
    """Set the spidev bufsiz the interface believes in."""
    def apply(value):
        monkeypatch.setattr(SpidevInterface, "read_bufsiz",
                            staticmethod(lambda: value))
    apply(4096)
    return apply


async def make_interface(device, **options):
    iface = SpidevInterface(device.path, device=device)
    for key, value in options.items():
        apply_option(iface, key, value)
    await iface.start()
    return iface


class TestStartup:
    async def test_reads_device_limits(self, bufsiz):
        bufsiz(1024)
        dev = FakeSpiDevice(max_speed_hz=500_000)
        iface = await make_interface(dev)
        assert iface.metadata["max_speed_hz"] == 500_000
        assert iface.metadata["bufsiz"] == 1024
        # The node's own ceiling becomes a cap like any other.
        assert iface.freq == 500_000

    async def test_fmax_option_is_capped_by_the_device(self, bufsiz):
        dev = FakeSpiDevice(max_speed_hz=500_000)
        iface = SpidevInterface(dev.path, device=dev)
        # Recorded before start(), i.e. before the fd exists.
        apply_option(iface, "fmax", "1M")
        await iface.start()
        assert iface.freq == 500_000

    async def test_fmax_below_device_ceiling_wins(self, bufsiz):
        dev = FakeSpiDevice(max_speed_hz=50_000_000)
        iface = await make_interface(dev, fmax="1M")
        assert iface.freq == 1_000_000

    async def test_device_tree_mode_bits_are_preserved(self, bufsiz):
        # SPI_3WIRE (0x10) is the board's business, not ours.
        dev = FakeSpiDevice(mode=0x10)
        iface = await make_interface(dev)
        assert iface.metadata["base_mode"] == 0x10

    async def test_stop_closes_the_device(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        await iface.stop()
        assert not dev.opened


class TestTransactions:
    async def test_single_transaction_is_one_message(self, bufsiz):
        dev = FakeSpiDevice(miso=b"\xef\x40\x18")
        iface = await make_interface(dev)
        target = iface.child_lookup("cs0")

        shifts = await target.transaction(
            spi.Shift(b"\x9f", read_miso=False), spi.Shift(3))

        assert len(dev.messages) == 1
        _mode, entry = dev.messages[0]
        assert [t["len"] for t in entry] == [1, 3]
        assert [t["reads"] for t in entry] == [False, True]
        assert entry[0]["mosi"] == b"\x9f"
        # Chip select drops at the end of the transaction.
        assert entry[-1]["cs_change"] == 0
        assert shifts[1].miso == b"\xef\x40\x18"

    async def test_bits_per_word_is_pinned_to_eight(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        await iface.child_lookup("cs0").transaction(spi.Shift(b"\x01\x02"))
        assert all(t["bits_per_word"] == 8 for t in dev.transfers)

    async def test_requested_speed_is_stamped_on_every_transfer(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev, fmax="1M")
        await iface.child_lookup("cs0").transaction(
            spi.Shift(b"\x01"), spi.Shift(2))
        assert all(t["speed_hz"] == 1_000_000 for t in dev.transfers)

    async def test_non_reading_shift_gets_no_rx_buffer(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        shifts = await iface.child_lookup("cs0").transaction(
            spi.Shift(b"\x06", read_miso=False))
        assert dev.transfers[0]["reads"] is False
        assert shifts[0].miso is None

    async def test_future_and_op_both_carry_miso(self, bufsiz):
        dev = FakeSpiDevice(miso=b"\xaa\xbb")
        iface = await make_interface(dev)
        op = spi.Shift(2)
        iface.post(spi.Cs(0, 0))
        future = iface.post(op)
        iface.post(spi.Cs(None))
        assert await future == b"\xaa\xbb"
        assert op.miso == b"\xaa\xbb"


class TestChunking:
    async def test_group_larger_than_bufsiz_splits_and_holds_cs(self, bufsiz):
        bufsiz(64)
        dev = FakeSpiDevice(miso=bytes(range(256)))
        iface = await make_interface(dev)

        shifts = await iface.child_lookup("cs0").transaction(spi.Shift(200))

        assert len(dev.messages) == 4
        assert [sum(t["len"] for t in entry)
                for _mode, entry in dev.messages] == [64, 64, 64, 8]
        # Every message but the last hands chip select to its successor.
        holds = [entry[-1]["cs_change"] for _mode, entry in dev.messages]
        assert holds == [1, 1, 1, 0]
        # MISO reassembles in order across the split.
        assert shifts[0].miso == bytes(range(200))

    async def test_transfer_count_is_capped_per_message(self, bufsiz):
        bufsiz(1 << 20)
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        await iface.child_lookup("cs0").transaction(
            *[spi.Shift(b"\x01", read_miso=False) for _ in range(600)])
        assert [len(entry) for _mode, entry in dev.messages] == [511, 89]

    async def test_message_boundary_does_not_disturb_short_shifts(self, bufsiz):
        bufsiz(4)
        dev = FakeSpiDevice(miso=b"\x11\x22\x33\x44\x55\x66")
        iface = await make_interface(dev)
        shifts = await iface.child_lookup("cs0").transaction(
            spi.Shift(b"\xaa\xbb\xcc", read_miso=False), spi.Shift(3))
        assert [sum(t["len"] for t in entry)
                for _mode, entry in dev.messages] == [4, 2]
        assert shifts[1].miso == b"\x11\x22\x33"


class TestMode:
    async def test_mode_is_written_only_when_it_changes(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        target = iface.child_lookup("cs0")

        await target.transaction(spi.Shift(b"\x01", read_miso=False))
        first = list(dev.mode_writes)
        target.mode = 3
        await target.transaction(spi.Shift(b"\x02", read_miso=False))
        second = list(dev.mode_writes)
        await target.transaction(spi.Shift(b"\x03", read_miso=False))

        assert first == [0]
        assert second == [0, 3]
        # Third transaction reuses mode 3, so no further write.
        assert dev.mode_writes == [0, 3]

    async def test_option_sets_the_target_mode(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev, mode="3")
        assert iface.child_lookup("cs0").mode == 3

    async def test_bad_mode_option_is_rejected(self, bufsiz):
        dev = FakeSpiDevice()
        iface = SpidevInterface(dev.path, device=dev)
        with pytest.raises(ValueError, match="0..3"):
            apply_option(iface, "mode", "4")


class TestUnbracketedShifts:
    async def test_shift_without_cs_asks_for_no_cs(self, bufsiz):
        dev = FakeSpiDevice(miso=b"\x5a")
        iface = await make_interface(dev)

        op = spi.Shift(1)
        assert await iface.post(op) == b"\x5a"

        mode, _entry = dev.messages[0]
        assert mode & SPI_NO_CS

    async def test_no_cs_is_cleared_for_the_next_transaction(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        await iface.post(spi.Shift(b"\x01", read_miso=False))
        await iface.child_lookup("cs0").transaction(
            spi.Shift(b"\x02", read_miso=False))
        assert dev.messages[0][0] & SPI_NO_CS
        assert not dev.messages[1][0] & SPI_NO_CS

    async def test_no_cs_option_disables_the_bit(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev, no_cs="false")
        await iface.post(spi.Shift(b"\x01", read_miso=False))
        assert not dev.messages[0][0] & SPI_NO_CS

    async def test_consecutive_unbracketed_shifts_share_one_message(
            self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        first = iface.post(spi.Shift(b"\x01", read_miso=False))
        second = iface.post(spi.Shift(b"\x02", read_miso=False))
        await asyncio.gather(first, second)
        assert len(dev.messages) == 1
        assert len(dev.messages[0][1]) == 2

    async def test_rejected_no_cs_retries_once_and_stops_asking(self, bufsiz):
        dev = FakeSpiDevice(fail_no_cs=True)
        iface = await make_interface(dev)

        await iface.post(spi.Shift(b"\x01", read_miso=False))
        assert dev.mode_writes == [0]
        assert not dev.messages[0][0] & SPI_NO_CS

        # The capability is remembered, so the bit is never asked for
        # again -- and mode 0 is already on the wire, so no second write.
        await iface.post(spi.Shift(b"\x02", read_miso=False))
        assert dev.mode_writes == [0]
        assert len(dev.messages) == 2


class TestCrossBatchGroups:
    async def test_group_left_open_survives_into_the_next_batch(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)

        iface.post(spi.Cs(0, 0))
        first = iface.post(spi.Shift(b"\x01", read_miso=False))
        await first

        second = iface.post(spi.Shift(b"\x02", read_miso=False))
        iface.post(spi.Cs(None))
        await second

        assert len(dev.messages) == 2
        # The first message keeps chip select for the second.
        assert dev.messages[0][1][-1]["cs_change"] == 1
        assert dev.messages[1][1][-1]["cs_change"] == 0
        # No SPI_NO_CS: these shifts are inside a group.
        assert not dev.messages[1][0] & SPI_NO_CS

    async def test_empty_chip_select_pulse_is_warned_not_faked(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        first = iface.post(spi.Cs(0, 0))
        second = iface.post(spi.Cs(None))
        assert await first is None
        assert await second is None
        assert dev.messages == []


class TestValidation:
    async def test_foreign_chip_select_rejects_the_batch(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        future = iface.post(spi.Cs(1, 0))
        with pytest.raises(ValueError, match="chip select 0 only"):
            await future
        assert dev.messages == []

    async def test_unaligned_bitstring_rejects_the_batch(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        future = iface.post(spi.Shift(BitString(0b10101, 5)))
        with pytest.raises(ValueError, match="whole bytes"):
            await future
        assert dev.messages == []

    async def test_unknown_op_rejects_the_batch(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        future = iface.post("nonsense")
        with pytest.raises(TypeError, match="cannot lower"):
            await future
        assert dev.messages == []

    async def test_validation_failure_fails_the_whole_batch(self, bufsiz):
        dev = FakeSpiDevice()
        iface = await make_interface(dev)
        good = iface.post(spi.Shift(b"\x01", read_miso=False))
        bad = iface.post(spi.Cs(7, 0))
        with pytest.raises(ValueError):
            await bad
        # A half-issued chip-select group is unrecoverable, so nothing
        # in the batch is allowed through.
        with pytest.raises(ValueError):
            await good
        assert dev.messages == []

    async def test_emsgsize_names_bufsiz(self, bufsiz):
        dev = FakeSpiDevice()
        dev.raise_on_message = OSError(errno.EMSGSIZE, "Message too long")
        iface = await make_interface(dev)
        with pytest.raises(OSError, match="bufsiz"):
            await iface.child_lookup("cs0").transaction(spi.Shift(4))


class TestSpiFlashIntegration:
    async def test_jedec_id_read_through_the_real_component(self, bufsiz):
        # RDID answers ef 40 18 (Winbond W25Q128); SFDP then reads back
        # the same pattern, which detect() rejects, falling back to the
        # JEDEC capacity byte.
        dev = FakeSpiDevice(miso=b"\xef\x40\x18")
        iface = await make_interface(dev)
        flash = await iface.child_summon("cs0", "flash")
        assert flash.jedec_id == 0xEF4018
        assert flash.total_size == 1 << 0x18


class TestEnumeration:
    async def test_lists_one_adapter_per_node(self, tmp_path):
        for name in ("spidev0.0", "spidev0.1", "spidev1.0"):
            (tmp_path / name).touch()
        enumerator = SpidevEnumerator(dev_glob=str(tmp_path / "spidev*"))
        assert [p.rsplit("/", 1)[1] for p in enumerator.paths()] == [
            "spidev0.0", "spidev0.1", "spidev1.0"]

    async def test_populate_is_idempotent(self, tmp_path):
        from acrobe.adapter.model import HwRoot
        (tmp_path / "spidev0.0").touch()
        root = HwRoot()
        enumerator = SpidevEnumerator(dev_glob=str(tmp_path / "spidev*"))
        await enumerator.populate(root)
        await enumerator.populate(root)
        assert [c.name for c in root.children] == ["spidev0.0"]

    async def test_adapter_advertises_the_spi_interface(self, tmp_path):
        adapter = SpidevAdapter("spidev0.0", "/dev/spidev0.0")
        assert adapter.child_hints() == ["spi"]
        assert adapter.ident == "/dev/spidev0.0"
        with pytest.raises(Exception):
            await adapter.child_spawn("i2c")


class TestStructLayout:
    def test_spi_ioc_transfer_is_32_bytes(self):
        assert ctypes.sizeof(SpiIocTransfer) == 32
        offsets = {name: getattr(SpiIocTransfer, name).offset
                   for name, _ in SpiIocTransfer._fields_}
        assert offsets == {
            "tx_buf": 0, "rx_buf": 8, "len": 16, "speed_hz": 20,
            "delay_usecs": 24, "bits_per_word": 26, "cs_change": 27,
            "tx_nbits": 28, "rx_nbits": 29, "word_delay_usecs": 30,
            "pad": 31,
        }

    def test_transfer_count_matches_spi_msgsize(self):
        # SPI_MSGSIZE collapses to 0 past the 14-bit size field.
        assert MAX_TRANSFERS == 511

