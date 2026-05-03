"""ST-Link USB command opcodes and response parsing.

Command framing: every command is a 16-byte OUT bulk transfer
(zero-padded if shorter), followed by a variable-length IN bulk
response. The first byte of each command identifies the command
group. Most debug-related commands sit under group 0xF2 (the
``DEBUG`` family).

Authoritative references:
* OpenOCD ``src/jtag/drivers/stlink_usb.c``
* pyOCD ``pyocd/probe/stlink/stlink.py``
* ST UM2448 / similar — public ST-Link protocol notes

This module captures only the constants we need; the actual USB
exchange lives in :mod:`.transport`.
"""

from __future__ import annotations

from dataclasses import dataclass


# Command-group prefixes (first byte of the 16-byte command frame).
CMD_GET_VERSION         = 0xF1
CMD_GET_CURRENT_MODE    = 0x41
CMD_DFU                 = 0xF3
CMD_DEBUG               = 0xF2
CMD_GET_VERSION_EXT     = 0xFB  # ST-Link v3 only

# DFU sub-commands (under CMD_DFU).
DFU_EXIT                = 0x07

# Current-mode return values.
MODE_DFU            = 0x00
MODE_MASS_STORAGE   = 0x01
MODE_DEBUG          = 0x02
MODE_SWIM           = 0x03
MODE_BOOTLOADER     = 0x04

_MODE_NAMES = {
    MODE_DFU:           "DFU",
    MODE_MASS_STORAGE:  "Mass Storage",
    MODE_DEBUG:         "Debug",
    MODE_SWIM:          "SWIM",
    MODE_BOOTLOADER:    "Bootloader",
}


def mode_name(mode: int) -> str:
    return _MODE_NAMES.get(mode, f"Unknown(0x{mode:02x})")


# DEBUG sub-commands (second byte of the frame, group=CMD_DEBUG).
# Phase-1 unused — listed here so phase 2 lands as a wiring change
# rather than re-discovering opcodes.
DEBUG_ENTER_JTAG_NORESET = 0xA4  # ST-Link v2/v3, no SRST asserted
DEBUG_ENTER_SWD_NORESET  = 0xA3
DEBUG_EXIT               = 0x21
DEBUG_READ_COREID        = 0x22  # legacy IDCODE read
DEBUG_GET_LAST_RW_STATUS = 0x3B
DEBUG_DRIVE_NRST         = 0x3C
DEBUG_APIV2_READ_DP_REG  = 0x45
DEBUG_APIV2_WRITE_DP_REG = 0x46
DEBUG_APIV2_READ_AP_REG  = 0x47
DEBUG_APIV2_WRITE_AP_REG = 0x48
DEBUG_APIV2_READ_MEM_32  = 0x07
DEBUG_APIV2_WRITE_MEM_32 = 0x08
DEBUG_APIV2_READ_MEM_16  = 0x47  # see notes — opcode varies by FW
DEBUG_APIV2_INIT_AP      = 0x4B  # ST-Link v3
DEBUG_APIV2_CLOSE_AP     = 0x4C  # ST-Link v3


# DEBUG_DRIVE_NRST argument values.
NRST_LOW   = 0x00
NRST_HIGH  = 0x01
NRST_PULSE = 0x02


@dataclass(frozen=True)
class StLinkVersion:
    """Decoded GET_VERSION / GET_VERSION_EXT response."""
    stlink: int        # major version (2 = v2, 3 = v3)
    jtag: int          # JTAG protocol version (firmware-defined)
    swim: int          # SWIM protocol version (or MSC for v3)
    msc: int           # mass-storage version
    bridge: int        # bridge protocol version (v3 only)
    vid: int
    pid: int

    def __str__(self):
        suffix = f" bridge=v{self.bridge}" if self.bridge else ""
        return (f"V{self.stlink}J{self.jtag}M{self.msc}"
                f"{suffix} (vid=0x{self.vid:04x} pid=0x{self.pid:04x})")


def parse_version_legacy(resp: bytes) -> StLinkVersion:
    """Parse a 6-byte response from CMD_GET_VERSION (v2-style).

    Layout: 16-bit big-endian word with stlink[15:12] / jtag[11:6]
    / swim[5:0] split, then VID and PID as little-endian 16-bit."""
    if len(resp) < 6:
        raise ValueError(f"GET_VERSION response too short: {len(resp)}")
    word = (resp[0] << 8) | resp[1]
    return StLinkVersion(
        stlink=(word >> 12) & 0xF,
        jtag=(word >> 6) & 0x3F,
        swim=word & 0x3F,
        msc=0,
        bridge=0,
        vid=resp[2] | (resp[3] << 8),
        pid=resp[4] | (resp[5] << 8),
    )


def parse_version_ext(resp: bytes) -> StLinkVersion:
    """Parse a 12-byte response from CMD_GET_VERSION_EXT (v3 only).

    Layout: byte 0 = stlink major, byte 1 = SWIM, byte 2 = JTAG,
    byte 3 = MSC, byte 4 = bridge, bytes 5-7 reserved, bytes 8-9 =
    VID (LE), bytes 10-11 = PID (LE)."""
    if len(resp) < 12:
        raise ValueError(f"GET_VERSION_EXT response too short: {len(resp)}")
    return StLinkVersion(
        stlink=resp[0],
        swim=resp[1],
        jtag=resp[2],
        msc=resp[3],
        bridge=resp[4],
        vid=resp[8] | (resp[9] << 8),
        pid=resp[10] | (resp[11] << 8),
    )
