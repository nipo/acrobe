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


# Top-level command groups (first byte of the 16-byte command frame).
CMD_GET_VERSION         = 0xF1
CMD_DEBUG               = 0xF2
CMD_DFU                 = 0xF3
CMD_SWIM                = 0xF4
CMD_GET_CURRENT_MODE    = 0xF5
CMD_GET_TARGET_VOLTAGE  = 0xF7
CMD_GET_VERSION_EXT     = 0xFB  # ST-Link v3 only

# DFU sub-commands (group=CMD_DFU).
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


# DEBUG sub-commands (group=CMD_DEBUG).
DEBUG_APIV2_ENTER          = 0x30
DEBUG_APIV2_READ_IDCODES   = 0x31
DEBUG_EXIT                 = 0x21
DEBUG_GET_LAST_RW_STATUS   = 0x3B
DEBUG_GET_LAST_RW_STATUS2  = 0x3E
DEBUG_DRIVE_NRST           = 0x3C
DEBUG_APIV2_READ_DAP_REG   = 0x45  # serves both DP and AP register reads
DEBUG_APIV2_WRITE_DAP_REG  = 0x46  # serves both DP and AP register writes
DEBUG_APIV2_INIT_AP        = 0x4B  # required before non-IDR AP access
DEBUG_APIV2_CLOSE_AP       = 0x4C
DEBUG_READMEM_32BIT        = 0x07  # bulk memory read through MEM-AP
DEBUG_WRITEMEM_32BIT       = 0x08  # bulk memory write through MEM-AP
DEBUG_READMEM_16BIT        = 0x47  # ST-Link v3 only
DEBUG_WRITEMEM_16BIT       = 0x48  # ST-Link v3 only
DEBUG_READMEM_8BIT         = 0x0C
DEBUG_WRITEMEM_8BIT        = 0x0D

# Mode-entry sub-commands (third byte after CMD_DEBUG / DEBUG_APIV2_ENTER).
ENTER_JTAG_NO_RESET = 0xA4
ENTER_SWD_NO_RESET  = 0xA3

# DRIVE_NRST argument values.
NRST_LOW   = 0x00
NRST_HIGH  = 0x01
NRST_PULSE = 0x02

# DAP-port pseudo-value used with READ_DAP_REG / WRITE_DAP_REG to
# address the DP itself (any other value is an AP index).
DAP_PORT_DP = 0xFFFF


# Status codes (response[0]) for DEBUG transactions.
DEBUG_ERR_OK              = 0x80
DEBUG_ERR_FAULT           = 0x81
SWD_AP_WAIT               = 0x10
SWD_AP_FAULT              = 0x11
SWD_AP_ERROR              = 0x12
SWD_AP_PARITY_ERROR       = 0x13
JTAG_GET_IDCODE_ERROR     = 0x09
JTAG_WRITE_ERROR          = 0x0c
JTAG_WRITE_VERIF_ERROR    = 0x0d
SWD_DP_WAIT               = 0x14
SWD_DP_FAULT              = 0x15
SWD_DP_ERROR              = 0x16
SWD_DP_PARITY_ERROR       = 0x17
SWD_AP_WDATA_ERROR        = 0x18
SWD_AP_STICKY_ERROR       = 0x19
SWD_AP_STICKYORUN_ERROR   = 0x1a
BAD_AP_ERROR              = 0x1d


_STATUS_NAMES = {
    DEBUG_ERR_OK:              "OK",
    DEBUG_ERR_FAULT:           "FAULT",
    SWD_AP_WAIT:               "SWD_AP_WAIT",
    SWD_AP_FAULT:              "SWD_AP_FAULT",
    SWD_AP_ERROR:              "SWD_AP_ERROR",
    SWD_AP_PARITY_ERROR:       "SWD_AP_PARITY_ERROR",
    JTAG_GET_IDCODE_ERROR:     "JTAG_GET_IDCODE_ERROR",
    JTAG_WRITE_ERROR:          "JTAG_WRITE_ERROR",
    JTAG_WRITE_VERIF_ERROR:    "JTAG_WRITE_VERIF_ERROR",
    SWD_DP_WAIT:               "SWD_DP_WAIT",
    SWD_DP_FAULT:              "SWD_DP_FAULT",
    SWD_DP_ERROR:              "SWD_DP_ERROR",
    SWD_DP_PARITY_ERROR:       "SWD_DP_PARITY_ERROR",
    SWD_AP_WDATA_ERROR:        "SWD_AP_WDATA_ERROR",
    SWD_AP_STICKY_ERROR:       "SWD_AP_STICKY_ERROR",
    SWD_AP_STICKYORUN_ERROR:   "SWD_AP_STICKYORUN_ERROR",
    BAD_AP_ERROR:              "BAD_AP_ERROR",
}


def status_name(status: int) -> str:
    return _STATUS_NAMES.get(status, f"Unknown(0x{status:02x})")


class StLinkError(Exception):
    """ST-Link returned a non-OK status for a DEBUG command."""

    def __init__(self, status: int, context: str = ""):
        self.status = status
        msg = f"ST-Link status {status_name(status)} (0x{status:02x})"
        if context:
            msg = f"{msg}: {context}"
        super().__init__(msg)


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
