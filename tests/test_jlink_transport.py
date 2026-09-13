"""Tests for `JLinkTransport` response framing.

The J-Link splits one logical response over several USB transfers:
JTAG_IO ends its TDO payload with a short packet and sends the
trailing status byte on its own, with a zero-length packet in between
when the payload is a multiple of the max packet size. Observed on a
J-Link V9 (firmware 2019-12-13), which answers a 1312-bit shift with
packets of 64, 64, 36 and 1 bytes.
"""

import logging

import pytest

from acrobe.adapter.jlink import protocol
from acrobe.adapter.jlink.transport import JLinkTransport


class MockBulkOut:
    def __init__(self):
        self.writes: list[bytes] = []

    async def write(self, data):
        self.writes.append(bytes(data))


class MockBulkIn:
    def __init__(self):
        self.__queue: list[bytes] = []
        self.reads: list[int] = []

    def queue(self, *chunks):
        self.__queue.extend(bytes(c) for c in chunks)

    async def read(self, size):
        self.reads.append(size)
        if not self.__queue:
            raise AssertionError("MockBulkIn empty — test under-queued data")
        return self.__queue.pop(0)


def make_transport(mps=64):
    ep_out = MockBulkOut()
    ep_in = MockBulkIn()
    transport = JLinkTransport(
        device=None, interface_index=0, ep_out=ep_out, ep_in=ep_in,
        mps=mps, logger=logging.getLogger("test.jlink"))
    return transport, ep_out, ep_in


class TestJtagIo:
    @pytest.mark.asyncio
    async def test_status_byte_in_its_own_transfer(self):
        transport, ep_out, ep_in = make_transport()
        transport.jtag_io_v3 = True
        tdo = bytes(range(164))
        ep_in.queue(tdo[:64], tdo[64:128], tdo[128:], b"\x00")

        got = await transport.jtag_io(b"\xff" * 164, b"\x00" * 164, 1312)

        assert got == tdo
        assert ep_out.writes == [
            bytes([protocol.CMD_JTAG_IO_V3, 0, 1312 & 0xFF, 1312 >> 8])
            + b"\xff" * 164 + b"\x00" * 164]

    @pytest.mark.asyncio
    async def test_zero_length_packet_before_status(self):
        transport, _, ep_in = make_transport()
        transport.jtag_io_v3 = True
        tdo = bytes(range(64))
        ep_in.queue(tdo, b"", b"\x00")

        assert await transport.jtag_io(b"\xff" * 64, b"\x00" * 64, 512) == tdo

    @pytest.mark.asyncio
    async def test_nonzero_status_raises(self):
        transport, _, ep_in = make_transport()
        transport.jtag_io_v3 = True
        ep_in.queue(b"\xa5", b"\x04")

        with pytest.raises(protocol.JLinkError):
            await transport.jtag_io(b"\xff", b"\x00", 8)

    @pytest.mark.asyncio
    async def test_v2_has_no_status_byte(self):
        transport, ep_out, ep_in = make_transport()
        transport.jtag_io_v3 = False
        ep_in.queue(b"\xa5")

        assert await transport.jtag_io(b"\xff", b"\x00", 8) == b"\xa5"
        assert ep_out.writes[0][0] == protocol.CMD_JTAG_IO_V2

    @pytest.mark.asyncio
    async def test_surplus_bytes_serve_the_next_read(self):
        """A transfer carrying more than the current command asks for
        keeps its tail for the next command rather than dropping it."""
        transport, _, ep_in = make_transport()
        transport.jtag_io_v3 = True
        ep_in.queue(b"\xa5\x00\x5a\x00")

        assert await transport.jtag_io(b"\xff", b"\x00", 8) == b"\xa5"
        assert await transport.jtag_io(b"\xff", b"\x00", 8) == b"\x5a"


class TestRegister:
    @pytest.mark.asyncio
    async def test_reads_minimum_response(self):
        transport, ep_out, ep_in = make_transport()
        resp = bytearray(protocol.REGISTER_MIN_SIZE)
        resp[0:8] = (0x0003).to_bytes(2, "little") \
            + (2).to_bytes(2, "little") \
            + (16).to_bytes(2, "little") \
            + (4).to_bytes(2, "little")
        ep_in.queue(bytes(resp[:64]), bytes(resp[64:]))

        assert await transport.register(True) == 3
        assert ep_out.writes[0][1] == 0x64

    @pytest.mark.asyncio
    async def test_reads_past_the_minimum_when_header_says_so(self):
        """8 + 8*16 + 4 = 140 bytes, beyond REGISTER_MIN_SIZE; the
        trailer must be consumed so the next command stays aligned."""
        transport, _, ep_in = make_transport()
        resp = bytearray(140)
        resp[0:8] = (0x0007).to_bytes(2, "little") \
            + (8).to_bytes(2, "little") \
            + (16).to_bytes(2, "little") \
            + (4).to_bytes(2, "little")
        ep_in.queue(bytes(resp[0:64]), bytes(resp[64:128]), bytes(resp[128:]),
                    b"\x01\x00\x00\x00")

        assert await transport.register(True) == 7
        assert await transport.get_caps() == b"\x01\x00\x00\x00"
