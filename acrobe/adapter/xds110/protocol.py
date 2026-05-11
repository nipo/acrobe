"""TI XDS110 wire protocol constants and frame helpers.

The XDS110 firmware speaks a custom request/response protocol over a
pair of USB bulk endpoints. Every transfer (both directions) is wrapped
in a 3-byte header — sync byte ``'*'`` (0x2a) followed by a little-endian
u16 payload size — and every response carries a 4-byte little-endian
u32 status code as the first word of its payload.

Authoritative reference: OpenOCD ``src/jtag/drivers/xds110.c``.

This module only carries constants and the frame encode/decode helpers;
the live USB exchange is in :mod:`.transport`."""

from __future__ import annotations

from dataclasses import dataclass
import enum

# Wire framing.
SYNC_BYTE = 0x2A           # '*' — first byte of every request and response
HEADER_LEN = 3             # sync + u16 payload size (LE)
ERROR_CODE_LEN = 4         # u32 LE status as first word of the response payload

# Firmware buffer sizes (must match the device).
MAX_DATA_BLOCK = 4096      # largest data payload per call
USB_PAYLOAD = MAX_DATA_BLOCK + 60  # data + opcode header + parameters
MAX_PACKET = 1024          # USB high-speed bulk MPS

# Status codes returned in the first 4 bytes of every response payload.
SC_ERR_NONE          = 0
SC_ERR_XDS110_FAIL   = -261
SC_ERR_SWD_WAIT      = -613
SC_ERR_SWD_FAULT     = -614
SC_ERR_SWD_PROTOCOL  = -615
SC_ERR_SWD_PARITY    = -616
SC_ERR_SWD_DEVICE_ID = -617

# Default retry / timeout values mirroring the OpenOCD driver.
DEFAULT_TIMEOUT_MS = 4000

# Stand-alone probe firmware identifier (returned by XDS_VERSION).
STAND_ALONE_HW_ID = 0x21

# Firmware version gates (u32 BCD-ish — compare numerically).
OCD_FIRMWARE_VERSION           = 0x02030011  # OCD_*_REQUEST batched APIs
FAST_TCK_FIRMWARE_VERSION      = 0x03000000  # 14 MHz peak TCK
FAST_TCK_PLUS_FIRMWARE_VERSION = 0x03000003  # 10/12 MHz fixed delays

# TCK frequency limits (kHz).
MIN_TCK_KHZ          = 100
MAX_SLOW_TCK_KHZ     = 2500
MAX_FAST_TCK_KHZ     = 14000
DEFAULT_TCK_KHZ      = 2500

# Fixed TCK delay-count values for "fast" frequencies. Below
# ``5500`` kHz the count is computed from a measured slope.
FAST_TCK_DELAY_14000 = 0
FAST_TCK_DELAY_12000 = 0xFFFFFFFE
FAST_TCK_DELAY_10000 = 0xFFFFFFFD
FAST_TCK_DELAY_8500  = 1
FAST_TCK_DELAY_5500  = 2

class Opcode(enum.IntEnum):
    """Opcodes (first byte of every command payload)."""
    XDS_CONNECT      = 0x01
    XDS_DISCONNECT   = 0x02
    XDS_VERSION      = 0x03
    XDS_SET_TCK      = 0x04
    XDS_SET_TRST     = 0x05
    XDS_CYCLE_TCK    = 0x07
    XDS_GOTO_STATE   = 0x09
    XDS_JTAG_SCAN    = 0x0C
    XDS_SET_SRST     = 0x0E
    CMAPI_CONNECT    = 0x0F
    CMAPI_DISCONNECT = 0x10
    CMAPI_ACQUIRE    = 0x11
    CMAPI_RELEASE    = 0x12
    CMAPI_REG_READ   = 0x15
    CMAPI_REG_WRITE  = 0x16
    SWD_CONNECT      = 0x17
    SWD_DISCONNECT   = 0x18
    CJTAG_CONNECT    = 0x2B
    CJTAG_DISCONNECT = 0x2C
    XDS_SET_SUPPLY   = 0x32
    OCD_DAP_REQUEST  = 0x3A
    OCD_SCAN_REQUEST = 0x3B
    OCD_PATHMOVE     = 0x3C

class JtagState(enum.IntEnum):
    """JTAG state IDs as understood by the XDS110 firmware."""
    RESET       = 1
    IDLE        = 2
    SHIFT_DR    = 3
    SHIFT_IR    = 4
    PAUSE_DR    = 5
    PAUSE_IR    = 6
    EXIT1_DR    = 8
    EXIT1_IR    = 9
    EXIT2_DR    = 10
    EXIT2_IR    = 11
    SELECT_DR   = 12
    SELECT_IR   = 13
    UPDATE_DR   = 14
    UPDATE_IR   = 15
    CAPTURE_DR  = 16
    CAPTURE_IR  = 17

class JtagTransit(enum.IntEnum):
    """Inter-state transit policies for GOTO_STATE / JTAG_SCAN."""
    QUICKEST    = 1
    VIA_CAPTURE = 2
    VIA_IDLE    = 3

# CJTAG_CONNECT mode argument.
MODE_JTAG = 1


class XDS110Error(Exception):
    """Raised when the firmware returns a non-zero status code, or when
    a response packet is malformed (bad sync byte, short header, payload
    length mismatch)."""

    def __init__(self, code: int, context: str = ""):
        self.code = code
        self.context = context
        super().__init__(f"XDS110 error {code} ({context})" if context
                         else f"XDS110 error {code}")


class Frame:
    """Encode and decode the XDS110 ``'*' + u16 size + payload`` frame.

    The host's command frame and the device's response frame share the
    same shape — only the payload differs. Encode is a single static
    method; decode is incremental because responses arrive split across
    multiple bulk transfers (USB HS limits each at MPS bytes)."""

    @staticmethod
    def encode(payload: bytes) -> bytes:
        """Wrap ``payload`` in a sync byte + LE size header. ``payload``
        must already start with the opcode byte."""
        n = len(payload)
        if n > USB_PAYLOAD:
            raise ValueError(
                f"XDS110 frame payload {n} bytes exceeds firmware "
                f"buffer ({USB_PAYLOAD} bytes)")
        return bytes([SYNC_BYTE, n & 0xFF, (n >> 8) & 0xFF]) + payload

    @staticmethod
    def parse_header(buf: bytes) -> int:
        """Validate the 3-byte response header and return the announced
        payload size. Raises :class:`XDS110Error` on a malformed header."""
        if len(buf) < HEADER_LEN:
            raise XDS110Error(SC_ERR_XDS110_FAIL,
                              f"short response header: {len(buf)}")
        if buf[0] != SYNC_BYTE:
            raise XDS110Error(SC_ERR_XDS110_FAIL,
                              f"bad sync byte 0x{buf[0]:02x}")
        size = buf[1] | (buf[2] << 8)
        if size < ERROR_CODE_LEN or size > USB_PAYLOAD:
            raise XDS110Error(SC_ERR_XDS110_FAIL,
                              f"bad payload size {size}")
        return size


class Bytes:
    """Little-endian integer pack / unpack used throughout the protocol."""

    @staticmethod
    def pack_u16(value: int) -> bytes:
        return bytes([value & 0xFF, (value >> 8) & 0xFF])

    @staticmethod
    def pack_u32(value: int) -> bytes:
        return bytes([value & 0xFF, (value >> 8) & 0xFF,
                      (value >> 16) & 0xFF, (value >> 24) & 0xFF])

    @staticmethod
    def unpack_u16(buf: bytes, off: int = 0) -> int:
        return buf[off] | (buf[off + 1] << 8)

    @staticmethod
    def unpack_u32(buf: bytes, off: int = 0) -> int:
        return (buf[off]
                | (buf[off + 1] << 8)
                | (buf[off + 2] << 16)
                | (buf[off + 3] << 24))

    @staticmethod
    def unpack_i32(buf: bytes, off: int = 0) -> int:
        v = Bytes.unpack_u32(buf, off)
        return v - 0x100000000 if v & 0x80000000 else v


@dataclass(frozen=True)
class Version:
    """Decoded XDS_VERSION response (firmware u32 + hardware u16)."""

    firmware: int
    hardware: int

    def __str__(self) -> str:
        # Firmware is packed as four BCD-ish bytes: maj.min.rev.build.
        b = (self.firmware >> 24) & 0xFF
        c = (self.firmware >> 16) & 0xFF
        d = (self.firmware >> 8) & 0xFF
        e = self.firmware & 0xFF
        return f"FW {b}.{c}.{d}.{e:02d} / HW 0x{self.hardware:04x}"


class TckDelay:
    """Convert a desired TCK frequency (Hz) to the firmware's
    ``delay_count`` parameter for XDS_SET_TCK.

    Two regimes, gated on firmware version:

    - "Slow" (FW < :data:`FAST_TCK_FIRMWARE_VERSION`): peak 2.5 MHz.
      Count is derived from a 66 ns per-step model on top of the
      pulse-width at the slow ceiling.
    - "Fast" (FW >= :data:`FAST_TCK_FIRMWARE_VERSION`): peak 14 MHz.
      Discrete fixed counts at 14/12/10/8.5/5.5 MHz; below 5.5 MHz a
      linear fit (slope 17.1e6, intercept -1.02) is used.

    Returns ``(delay_count, achieved_khz)`` so the caller can both push
    the count to firmware and report the actual frequency upstream
    through :class:`acrobe.freq_capper.FreqCapper`."""

    SLOW_STEP_NS = 66.0
    FAST_FIT_SLOPE = 17_100_000.0
    FAST_FIT_INTERCEPT = -1.02

    @classmethod
    def for_freq(cls, freq_hz: int, firmware: int) -> tuple[int, int]:
        speed_khz = max(MIN_TCK_KHZ, freq_hz // 1000)

        if firmware < FAST_TCK_FIRMWARE_VERSION:
            return cls._slow(speed_khz)
        return cls._fast(speed_khz, firmware)

    @classmethod
    def _slow(cls, speed_khz: int) -> tuple[int, int]:
        if speed_khz >= MAX_SLOW_TCK_KHZ:
            return 0, MAX_SLOW_TCK_KHZ

        one_giga = 1_000_000_000.0
        max_pulse_ns = one_giga / (MAX_SLOW_TCK_KHZ * 1000)
        target_pulse_ns = one_giga / (speed_khz * 1000)
        # Walk the count up until the pulse exceeds the requested period,
        # then pick whichever of (count, count-1) lands closest.
        count = 0
        current = max_pulse_ns
        while current < target_pulse_ns:
            current += cls.SLOW_STEP_NS
            count += 1
        if count:
            f1 = (speed_khz * 1000) - one_giga / (
                max_pulse_ns + cls.SLOW_STEP_NS * count)
            f2 = one_giga / (
                max_pulse_ns + cls.SLOW_STEP_NS * (count - 1)) - (
                speed_khz * 1000)
            if f1 > f2:
                count -= 1
        achieved_hz = one_giga / (max_pulse_ns + cls.SLOW_STEP_NS * count)
        return count, int(achieved_hz / 1000)

    @classmethod
    def _fast(cls, speed_khz: int, firmware: int) -> tuple[int, int]:
        if speed_khz >= MAX_FAST_TCK_KHZ:
            return FAST_TCK_DELAY_14000, MAX_FAST_TCK_KHZ
        if speed_khz >= 12000 and firmware >= FAST_TCK_PLUS_FIRMWARE_VERSION:
            return FAST_TCK_DELAY_12000, 12000
        if speed_khz >= 10000 and firmware >= FAST_TCK_PLUS_FIRMWARE_VERSION:
            return FAST_TCK_DELAY_10000, 10000
        if speed_khz >= 8500:
            return FAST_TCK_DELAY_8500, 8500
        if speed_khz >= 5500:
            return FAST_TCK_DELAY_5500, 5500

        period_s = 1.0 / (speed_khz * 1000)
        delay = cls.FAST_FIT_SLOPE * period_s + cls.FAST_FIT_INTERCEPT
        count = 1 if delay < 1.0 else int(delay)
        achieved_hz = (count - cls.FAST_FIT_INTERCEPT) / cls.FAST_FIT_SLOPE
        # Invert the linear fit: target_freq = 1/(slope*period+intercept)
        # gives delay; achieved is the freq for that integer count.
        achieved_hz = 1.0 / ((count - cls.FAST_FIT_INTERCEPT)
                             / cls.FAST_FIT_SLOPE)
        return count, int(achieved_hz / 1000)
