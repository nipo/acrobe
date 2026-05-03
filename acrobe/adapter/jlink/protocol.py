"""J-Link USB protocol opcodes and capability bits.

Authoritative reference: OpenOCD's libjaylink
(``src/jtag/drivers/libjaylink/libjaylink/*.c``). The protocol is
single-byte command opcodes with variable-length arguments and
responses; framing lives in :mod:`.transport`.
"""

from __future__ import annotations


# --- Command opcodes -----------------------------------------------

CMD_GET_VERSION         = 0x01  # firmware version string (length-prefixed)
CMD_SET_SPEED           = 0x05  # JTAG/SWD clock speed in kHz (LE u16)
CMD_GET_HW_STATUS       = 0x07  # 8-byte status struct
CMD_SET_TARGET_POWER    = 0x08
CMD_GET_SPEEDS          = 0xC0  # 6-byte: base_freq + min_div
CMD_GET_HW_INFO         = 0xC1  # 4-byte hardware-info field bitmap arg
CMD_SELECT_TIF          = 0xC7  # set/get target interface (JTAG / SWD / ...)
CMD_JTAG_IO_V2          = 0xCE  # bit-bang JTAG (no status response)
CMD_JTAG_IO_V3          = 0xCF  # bit-bang JTAG (with status byte)
                                # 0xCF is also CMD_SWD_IO when TIF=SWD.
CMD_CLEAR_RESET         = 0xDC  # deassert nRST
CMD_SET_RESET           = 0xDD  # assert nRST
CMD_GET_CAPS            = 0xE8  # 32-bit capability bitfield
CMD_GET_EXT_CAPS        = 0xED  # 256-bit extended capabilities
CMD_GET_HW_VERSION      = 0xF0  # 4-byte hardware version (LE u32)


# --- Target interface (TIF) selectors ------------------------------

TIF_JTAG    = 0
TIF_SWD     = 1
TIF_BDM3    = 2
TIF_FINE    = 3

_TIF_NAMES = {
    TIF_JTAG: "JTAG", TIF_SWD: "SWD",
    TIF_BDM3: "BDM3", TIF_FINE: "FINE",
}


def tif_name(tif: int) -> str:
    return _TIF_NAMES.get(tif, f"unknown({tif})")


# --- Capability bits -----------------------------------------------
#
# The 32-bit GET_CAPS response (and 256-bit GET_EXT_CAPS) is a
# bitfield. Each bit position corresponds to one capability — only
# the few we use today are named here. Add as needed.

CAP_RESERVED              = 0
CAP_GET_HW_VERSION        = 1   # CMD_GET_HW_VERSION supported
CAP_WRITE_DCC             = 2
CAP_ADAPTIVE_CLOCKING     = 3
CAP_READ_CONFIG           = 4
CAP_WRITE_CONFIG          = 5
CAP_TRACE                 = 6
CAP_WRITE_MEM             = 7
CAP_READ_MEM              = 8
CAP_SPEED_INFO            = 9   # CMD_GET_SPEEDS supported
CAP_EXEC_CODE             = 10
CAP_GET_MAX_BLOCK_SIZE    = 11
CAP_GET_HW_INFO           = 12
CAP_SET_KS_POWER          = 13
CAP_RESET_STOP_TIMED      = 14
CAP_MEASURE_RTCK_REACT    = 16
CAP_SELECT_IF             = 17  # CMD_SELECT_TIF supported
CAP_RW_MEM_ARM79          = 18
CAP_GET_COUNTERS          = 19
CAP_READ_DCC              = 20
CAP_GET_CPU_CAPS          = 21
CAP_EXEC_CPU_CMD          = 22
CAP_SWO                   = 23
CAP_WRITE_DCC_EX          = 24
CAP_UPDATE_FIRMWARE_EX    = 25
CAP_FILE_IO               = 26
CAP_REGISTER              = 27
CAP_INDICATORS            = 28
CAP_TEST_NET_SPEED        = 29
CAP_RAWTRACE              = 30
CAP_GET_EXT_CAPS          = 31


def has_cap(caps: bytes, bit: int) -> bool:
    """Test whether capability bit ``bit`` is set in the
    little-endian byte-array returned by GET_CAPS."""
    if bit // 8 >= len(caps):
        return False
    return bool(caps[bit // 8] & (1 << (bit % 8)))


class JLinkError(Exception):
    """J-Link returned a non-OK status for a command."""
