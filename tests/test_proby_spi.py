"""Proby SPI transactor stack, faked at the FT245 pipe boundary.

A `FakeProby` plays the device side of the ``jtag_swd_i2c`` firmware:
it un-chunks what the host writes, dispatches the routed frame to the
control-status or the SPI transactor model, and chunks the response
back. `FakePipe` glues that to the `Pipe` byte-stream contract, so the
whole host stack (Chunked → Router → FramedEndpoint → codec) is
exercised without FTDI or USB.
"""

import pytest

from acrobe.adapter.proby.transactors import (
    ProbySpiInterface, BASE_FREQ_REG, MODE_REG, MODE_SPI, MODE_SPI_INV,
    CS_DST, SPI_DST, LOCAL_ID,
)
from acrobe.component.nsl.bnoc.chunked import Chunked
from acrobe.protocol.pipe import Pipe, Read, Write
from acrobe.protocol.spi import Cs, Shift


class FakeProby:
    """Device side of the ``jtag_swd_i2c`` firmware, minus the wire."""

    BASE_FREQ = 24_000_000

    def __init__(self, miso: bytes = b""):
        self.regs = {BASE_FREQ_REG: self.BASE_FREQ}
        self.reg_writes: list[tuple[int, int]] = []
        self.spi_commands: list[bytes] = []
        self.__miso = bytearray(miso)
        self.__buf = bytearray()
        self.__frame = bytearray()

    def feed(self, data: bytes) -> bytes:
        """Consume host bytes, return whatever the device answers."""
        self.__buf += data
        out = bytearray()
        while self.__buf:
            b0 = self.__buf[0]
            if b0 & Chunked.CTRL_FLAG:
                del self.__buf[0]
                self.__frame.clear()
                continue
            if len(self.__buf) < 2:
                break
            size = (((b0 & Chunked.SIZE_HI_MASK) << 8) | self.__buf[1]) + 1
            if len(self.__buf) < 2 + size:
                break
            self.__frame += self.__buf[2:2 + size]
            del self.__buf[:2 + size]
            if b0 & Chunked.LAST_FLAG:
                out += self.__chunk(self.__answer(bytes(self.__frame)))
                self.__frame.clear()
        return bytes(out)

    @staticmethod
    def __chunk(frame: bytes) -> bytes:
        n = len(frame) - 1
        return bytes([Chunked.LAST_FLAG | ((n >> 8) & Chunked.SIZE_HI_MASK),
                      n & 0xff]) + frame

    def __answer(self, frame: bytes) -> bytes:
        header, tag, payload = frame[0], frame[1], frame[2:]
        destination, source = header & 0xf, header >> 4
        if destination == CS_DST:
            rsp = self.__control_status(payload)
        elif destination == SPI_DST:
            self.spi_commands.append(payload)
            rsp = self.__spi(payload)
        else:
            raise AssertionError(f"unexpected routing destination {destination}")
        return bytes([source | (destination << 4), tag]) + rsp

    def __control_status(self, cmd: bytes) -> bytes:
        rsp = bytearray()
        offset = 0
        while offset < len(cmd):
            byte = cmd[offset]
            offset += 1
            reg = byte & 0x7f
            if byte & 0x80:
                rsp.append(0)
                rsp += int(self.regs.get(reg, 0)).to_bytes(4, "little")
                continue
            value = int.from_bytes(cmd[offset:offset + 4], "little")
            offset += 4
            self.reg_writes.append((reg, value))
            self.regs[reg] = value
            rsp.append(0)
        return bytes(rsp)

    def __spi(self, cmd: bytes) -> bytes:
        rsp = bytearray()
        offset = 0
        while offset < len(cmd):
            byte = cmd[offset]
            offset += 1
            rsp.append(0)
            if byte < 0x40:
                continue
            count = (byte & 0x3f) + 1
            if byte >= 0x80:
                offset += count
            if byte & 0x40:
                rsp += self.__miso[:count]
                del self.__miso[:count]
        return bytes(rsp)


class FakePipe(Pipe):
    """Byte pipe wired to a `FakeProby`. Reads park until the device
    has produced enough bytes to satisfy them."""

    def __init__(self, device: FakeProby, name: str = "fake-pipe"):
        super().__init__(name)
        self.__device = device
        self.__rx = bytearray()
        self.__waiters: list = []

    async def flush_ops(self, batch):
        for op, future in batch:
            if isinstance(op, Write):
                self.__rx += self.__device.feed(op.data)
                if future is not None:
                    future.set_result(None)
            elif isinstance(op, Read):
                self.__waiters.append((op.size, future))
            else:
                raise AssertionError(f"unexpected pipe op {op!r}")
        self.__service()

    def __service(self):
        while self.__waiters and len(self.__rx) >= self.__waiters[0][0]:
            size, future = self.__waiters.pop(0)
            data = bytes(self.__rx[:size])
            del self.__rx[:size]
            if future is not None and not future.done():
                future.set_result(data)


async def make_interface(device, **options):
    interface = ProbySpiInterface(FakePipe(device))
    for key, value in options.items():
        # The MRO-walking dispatch child_summon uses for path options,
        # so mixin-handled keys (fmax) land too.
        interface._Node__option_set(key, value)
    await interface.start_tree()
    return interface


class TestProbySpiBringUp:
    @pytest.mark.asyncio
    async def test_reads_base_freq_and_writes_mode(self):
        device = FakeProby()
        await make_interface(device)
        assert device.reg_writes == [(MODE_REG, MODE_SPI)]

    @pytest.mark.asyncio
    async def test_inverted_pinout_selects_alternate_mux(self):
        device = FakeProby()
        await make_interface(device, pinout="inverted")
        assert device.reg_writes == [(MODE_REG, MODE_SPI_INV)]

    @pytest.mark.asyncio
    async def test_unknown_pinout_rejected(self):
        with pytest.raises(ValueError):
            ProbySpiInterface(FakePipe(FakeProby())).option_set(
                "pinout", "sideways")

    @pytest.mark.asyncio
    async def test_unrelated_option_ignored(self):
        device = FakeProby()
        interface = ProbySpiInterface(FakePipe(device))
        interface.option_set("whatever", "1")
        await interface.start_tree()
        assert device.reg_writes == [(MODE_REG, MODE_SPI)]

    @pytest.mark.asyncio
    async def test_target_child_on_cs0(self):
        interface = await make_interface(FakeProby())
        assert interface.child_lookup("cs0") is not None

    @pytest.mark.asyncio
    async def test_freq_update_before_start(self):
        assert ProbySpiInterface(FakePipe(FakeProby())).freq_update(1e6) == 0.0

    @pytest.mark.asyncio
    async def test_fmax_option_reaches_the_wire(self):
        device = FakeProby()
        interface = await make_interface(device, fmax="1M")

        await interface.post(Cs(None))
        assert interface.freq == pytest.approx(1e6)
        # divisor 11 -> DIVH 1, DIVL 3.
        assert device.spi_commands == [bytes([0x21, 0x33, 0x07])]


class TestProbySpiTraffic:
    @pytest.mark.asyncio
    async def test_batch_encoding_and_miso(self):
        device = FakeProby(miso=b"\xff\xef\x40\x18")
        interface = await make_interface(device)

        select = interface.post(Cs(0, mode=0))
        write_only = interface.post(Shift(b"\x06", read_miso=False))
        read_back = interface.post(Shift(b"\x9f\x00\x00\x00",
                                         read_miso=True))
        deselect = interface.post(Cs(None))

        assert await read_back == b"\xff\xef\x40\x18"
        assert await select is None
        assert await write_only is None
        assert await deselect is None

        # base_freq 24 MHz -> divisor 23 -> DIVH 23>>3, DIVL 23&7.
        assert device.spi_commands == [
            bytes([0x22, 0x37,
                   0x00,
                   0x80, 0x06,
                   0xc3, 0x9f, 0x00, 0x00, 0x00,
                   0x07])
        ]

    @pytest.mark.asyncio
    async def test_divisor_emitted_once(self):
        device = FakeProby(miso=b"\x5a" * 4)
        interface = await make_interface(device)

        await interface.post(Shift(b"\x01\x02", read_miso=True))
        assert await interface.post(Shift(b"\x03\x04", read_miso=True)) \
            == b"\x5a\x5a"

        assert device.spi_commands == [
            bytes([0x22, 0x37, 0xc1, 0x01, 0x02]),
            bytes([0xc1, 0x03, 0x04]),
        ]

    @pytest.mark.asyncio
    async def test_mode_bits_reach_select_command(self):
        device = FakeProby()
        interface = await make_interface(device)

        await interface.post(Cs(0, mode=3))
        assert device.spi_commands[-1][-1] == 0x18

    @pytest.mark.asyncio
    async def test_freq_update_reprograms_divisor(self):
        device = FakeProby()
        interface = await make_interface(device)

        assert interface.freq_update(1e6) == pytest.approx(1e6)
        await interface.post(Cs(None))
        # divisor 11 -> DIVH 1, DIVL 3.
        assert device.spi_commands == [bytes([0x21, 0x33, 0x07])]

    @pytest.mark.asyncio
    async def test_target_transaction_brackets_shift_with_cs(self):
        device = FakeProby(miso=b"\x20\xba")
        interface = await make_interface(device)
        target = interface.child_lookup("cs0")

        shifts = await target.transaction(Shift(b"\x9f\x00", read_miso=True))
        assert shifts[0].miso == b"\x20\xba"
        assert device.spi_commands == [
            bytes([0x22, 0x37, 0x00, 0xc1, 0x9f, 0x00, 0x07])
        ]


class TestProbySpiRouting:
    @pytest.mark.asyncio
    async def test_spi_frames_carry_the_spi_destination(self):
        device = FakeProby()
        interface = await make_interface(device)

        written = []
        device_feed = device.feed
        device.feed = lambda data: (written.append(data), device_feed(data))[1]

        await interface.post(Cs(None))

        # Chunk header (2 bytes), then the routing header byte.
        assert b"".join(written)[2] == (SPI_DST | (LOCAL_ID << 4))
