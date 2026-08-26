"""ioctl request-number encoding (`acrobe.adapter.linux.ioctl`).

The expected values are the constants other parts of the tree already
hand-computed, so this doubles as a cross-check against them.
"""

import ctypes
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux ioctl ABI")

from acrobe.adapter.linux.ioctl import Ioctl  # noqa: E402


class TestIoctlEncoding:
    @pytest.mark.parametrize("expected,call", [
        # linux/spi/spidev.h
        (0x80016B01, ("read", "k", 1, 1)),      # SPI_IOC_RD_MODE
        (0x40016B01, ("write", "k", 1, 1)),     # SPI_IOC_WR_MODE
        (0x80016B02, ("read", "k", 2, 1)),      # SPI_IOC_RD_LSB_FIRST
        (0x80046B04, ("read", "k", 4, 4)),      # SPI_IOC_RD_MAX_SPEED_HZ
        (0x40046B05, ("write", "k", 5, 4)),     # SPI_IOC_WR_MODE32
        (0x40206B00, ("write", "k", 0, 32)),    # SPI_IOC_MESSAGE(1)
        (0x40406B00, ("write", "k", 0, 64)),    # SPI_IOC_MESSAGE(2)
        # acrobe/adapter/stream_endpoint.py's GET_INFO
        (0x8018EA00, ("read", 0xEA, 0, 24)),
        # acrobe/adapter/tty.py's TCGETS2 / TCSETS2
        (0x802C542A, ("read", "T", 0x2A, 44)),
        (0x402C542B, ("write", "T", 0x2B, 44)),
    ])
    def test_known_constants(self, expected, call):
        method, magic, nr, size = call
        assert getattr(Ioctl, method)(magic, nr, size) == expected

    def test_none_has_no_size_or_direction(self):
        assert Ioctl.none("k", 3) == 0x00006B03

    def test_read_write_sets_both_directions(self):
        assert Ioctl.read_write("k", 1, 4) >> Ioctl.DIRSHIFT == 3

    def test_magic_accepts_int_and_char(self):
        assert Ioctl.read(0x6B, 1, 1) == Ioctl.read("k", 1, 1)

    def test_oversized_argument_is_rejected(self):
        # SPI_IOC_MESSAGE(512) is exactly where SPI_MSGSIZE collapses.
        with pytest.raises(ValueError, match="size field"):
            Ioctl.write("k", 0, 512 * 32)
