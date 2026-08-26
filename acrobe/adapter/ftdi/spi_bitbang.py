from __future__ import annotations

import asyncio
import logging

from .transport import FtdiTransport
from ...log import PROTOCOL


class SpiBitbang:
    """Write-only mode-0 SPI shifter over FTDI synchronous bitbang.

    Synchronous bitbang drives every pin of the channel from each
    byte written and samples them all on the way back, so the data
    output pin is a free choice — unlike MPSSE, whose MOSI is
    hardwired. That is the reason this class exists: on boards where
    the device to feed hangs off the net MPSSE calls MISO, MPSSE
    cannot drive it at all.

    Reading MISO is not supported: the shifter has no notion of an
    input data pin, only of named status inputs sampled with
    `input_get` or alongside dummy clocks.

    One SPI bit costs two bytes on the wire (clock low, then clock
    high). Data is presented while the clock is low and is valid on
    the rising edge, MSB first.
    """

    # Chunk of payload bytes turned into one write/read round trip.
    PAYLOAD_CHUNK = 512

    # The FTDI byte clock in bitbang mode is the baud generator
    # output multiplied by a chip- and mode-dependent factor (up to
    # 16), and `FtdiTransport.set_baudrate` encodes the divisor for
    # non-H parts, which an H part interprets 4 times faster. A
    # requested baud rate can therefore come out up to 64 times
    # higher than asked; `bit_freq_cap` derates by that much so a
    # frequency ceiling is a ceiling on every part.
    CLOCK_UNCERTAINTY = 64

    def __init__(self, transport, *, sck: int, mosi: int, csn: int,
                 outputs=None, inputs=None, name: str = "spi-bitbang"):
        self.__transport = transport
        self.__sck = 1 << sck
        self.__mosi = 1 << mosi
        self.__csn = 1 << csn
        self.__outputs = {n: 1 << p for n, p in (outputs or {}).items()}
        self.__inputs = {n: 1 << p for n, p in (inputs or {}).items()}
        self.__value = 0
        self.logger = logging.getLogger(name)

    @staticmethod
    def oe_mask(*, sck: int, mosi: int, csn: int, outputs=None) -> int:
        """Output-enable mask for `FtdiTransport.from_device_bitbang`."""
        mask = (1 << sck) | (1 << mosi) | (1 << csn)
        for pin in (outputs or {}).values():
            mask |= 1 << pin
        return mask

    @classmethod
    async def open(cls, device, *, interface_index: int = 0,
                   sck: int, mosi: int, csn: int,
                   outputs=None, inputs=None, name: str = "spi-bitbang"):
        """Switch one channel of an opened FTDI device to synchronous
        bitbang and return a shifter driving it.

        Every declared output starts low, chip select included."""
        transport = await FtdiTransport.from_device_bitbang(
            device, interface_index=interface_index,
            oe_mask=cls.oe_mask(sck=sck, mosi=mosi, csn=csn,
                                outputs=outputs))
        return cls(transport, sck=sck, mosi=mosi, csn=csn,
                   outputs=outputs, inputs=inputs, name=name)

    @property
    def transport(self):
        return self.__transport

    async def close(self):
        await self.__transport.close()

    async def bit_freq_cap(self, freq: float) -> float:
        """Set the baud generator so the shifted bit clock stays
        below `freq` on any FTDI part. Returns the requested baud
        rate."""
        baudrate = 2 * freq / self.CLOCK_UNCERTAINTY
        await self.__transport.set_baudrate(baudrate)
        return baudrate

    async def output_set(self, name: str, level: int):
        """Drive one declared output pin."""
        self.__level_set(self.__pin(self.__outputs, name), level)
        await self.__transfer(bytes([self.__value]))

    async def cs_set(self, asserted: bool):
        """Drive chip select. Active low."""
        self.__level_set(self.__csn, 0 if asserted else 1)
        await self.__transfer(bytes([self.__value]))

    async def input_get(self, name: str) -> bool:
        """Sample one declared input pin without clocking."""
        mask = self.__pin(self.__inputs, name)
        rsp = await self.__transfer(bytes([self.__value]))
        return bool(rsp[0] & mask)

    async def wait(self, seconds: float):
        """Host-side pause between transfers. Sequencing a
        configuration handshake needs real time to pass with the pins
        held where the last transfer left them."""
        await asyncio.sleep(seconds)

    async def shift(self, data: bytes, *, read_miso: bool = False):
        """Clock `data` out, MSB first."""
        if read_miso:
            raise NotImplementedError(
                "sync bitbang shifter is write-only: no MISO pin is sampled")
        for offset in range(0, len(data), self.PAYLOAD_CHUNK):
            chunk = data[offset:offset + self.PAYLOAD_CHUNK]
            await self.__transfer(self.__shift_bytes(chunk))

    async def clocks(self, count: int, sample: str | None = None):
        """Run `count` clock cycles with the data pin held at its
        current level. When `sample` names an input pin, returns the
        list of its values, one per cycle, sampled with the byte that
        raises the clock; otherwise returns None."""
        mask = self.__pin(self.__inputs, sample) if sample else None
        rsp = await self.__transfer(self.__clock_bytes(count))
        if mask is None:
            return None
        # Clock-high bytes sit at odd offsets, one pair per cycle.
        return [bool(rsp[i] & mask) for i in range(1, 2 * count, 2)]

    # --- Byte-stream construction ---

    def __shift_bytes(self, data: bytes) -> bytes:
        buf = bytearray()
        base = self.__value & ~(self.__sck | self.__mosi)
        last = base
        for byte in data:
            for bit in range(7, -1, -1):
                last = base | (self.__mosi if (byte >> bit) & 1 else 0)
                buf.append(last)
                buf.append(last | self.__sck)
        buf.append(last)
        self.__value = last
        return bytes(buf)

    def __clock_bytes(self, count: int) -> bytes:
        buf = bytearray()
        low = self.__value & ~self.__sck
        for _ in range(count):
            buf.append(low)
            buf.append(low | self.__sck)
        buf.append(low)
        self.__value = low
        return bytes(buf)

    def __level_set(self, mask: int, level: int):
        if level:
            self.__value |= mask
        else:
            self.__value &= ~mask

    @staticmethod
    def __pin(pins, name):
        try:
            return pins[name]
        except KeyError:
            raise KeyError(f"undeclared pin {name!r}") from None

    async def __transfer(self, data: bytes) -> bytes:
        """Write the bitbang stream and collect the pin samples it
        produces. Synchronous bitbang returns one byte per byte
        written; the read must be drained or the device stalls."""
        self.logger.log(PROTOCOL, "BB >> %d bytes", len(data))
        _, rsp = await asyncio.gather(
            self.__transport.write(data),
            self.__transport.read(len(data)))
        return rsp
