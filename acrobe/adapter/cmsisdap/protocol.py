"""CMSIS-DAP protocol opcodes, identifiers, and constants.

Reference: ARM CMSIS-DAP specification, Doc 5.2.0 (matches the
``DAP_Info(version)`` strings shipped on most modern adapters).
The protocol is a request/response byte stream — first byte is the
command, response echoes the same command followed by a status
byte and any return payload."""

from __future__ import annotations


# --- Command opcodes -----------------------------------------------

# General
CMD_INFO              = 0x00
CMD_HOST_STATUS       = 0x01
CMD_CONNECT           = 0x02
CMD_DISCONNECT        = 0x03
CMD_TRANSFER_CONFIGURE = 0x04
CMD_TRANSFER          = 0x05
CMD_TRANSFER_BLOCK    = 0x06
CMD_TRANSFER_ABORT    = 0x07
CMD_WRITE_ABORT       = 0x08
CMD_DELAY             = 0x09
CMD_RESET_TARGET      = 0x0A

# SWJ (shared)
CMD_SWJ_PINS          = 0x10
CMD_SWJ_CLOCK         = 0x11
CMD_SWJ_SEQUENCE      = 0x12

# SWD-specific
CMD_SWD_CONFIGURE     = 0x13
CMD_SWD_SEQUENCE      = 0x1D

# JTAG-specific
CMD_JTAG_SEQUENCE     = 0x14
CMD_JTAG_CONFIGURE    = 0x15
CMD_JTAG_IDCODE       = 0x16

# SWO (Single Wire Output / trace)
CMD_SWO_TRANSPORT     = 0x17
CMD_SWO_MODE          = 0x18
CMD_SWO_BAUDRATE      = 0x19
CMD_SWO_CONTROL       = 0x1A
CMD_SWO_STATUS        = 0x1B
CMD_SWO_DATA          = 0x1C

# Vendor extension range: 0x80..0x9F


# --- DAP_Info(id) IDs ----------------------------------------------

INFO_VENDOR_NAME      = 0x01
INFO_PRODUCT_NAME     = 0x02
INFO_SERIAL_NUMBER    = 0x03
INFO_FW_VERSION       = 0x04
INFO_DEVICE_VENDOR    = 0x05
INFO_DEVICE_NAME      = 0x06
INFO_BOARD_VENDOR     = 0x07
INFO_BOARD_NAME       = 0x08
INFO_FW_VERSION_PROD  = 0x09
INFO_CAPABILITIES     = 0xF0
INFO_TEST_DOMAIN_TIMER = 0xF1
INFO_UART_RX_BUFFER_SIZE = 0xFB
INFO_UART_TX_BUFFER_SIZE = 0xFC
INFO_SWO_TRACE_BUFFER_SIZE = 0xFD
INFO_PACKET_COUNT     = 0xFE
INFO_PACKET_SIZE      = 0xFF


# --- DAP_Info(CAPABILITIES) bit positions --------------------------

CAP_SWD               = 1 << 0
CAP_JTAG              = 1 << 1
CAP_SWO_UART          = 1 << 2
CAP_SWO_MANCHESTER    = 1 << 3
CAP_ATOMIC_CMDS       = 1 << 4
CAP_TEST_DOMAIN_TIMER = 1 << 5
CAP_SWO_STREAM        = 1 << 6
CAP_UART_PORT         = 1 << 7
CAP_USB_COM_PORT      = 1 << 8


# --- DAP_Connect port codes ----------------------------------------

PORT_DEFAULT          = 0
PORT_SWD              = 1
PORT_JTAG             = 2


# --- DAP_HostStatus types ------------------------------------------

HOST_STATUS_CONNECT   = 0
HOST_STATUS_RUNNING   = 1


# --- DAP_Transfer request bits -------------------------------------

XFER_APnDP            = 1 << 0   # 0=DP, 1=AP
XFER_RnW              = 1 << 1   # 0=Write, 1=Read
XFER_A2               = 1 << 2
XFER_A3               = 1 << 3
XFER_VALUE_MATCH      = 1 << 4   # for reads: AP read with match-mask
XFER_MATCH_MASK       = 1 << 5   # write the match mask
XFER_TIMESTAMP        = 1 << 7

# --- DAP_Transfer response bits (per-transfer) ---------------------

# Low 3 bits = ACK; bits 3+ = error flags.
ACK_OK                = 0b001
ACK_WAIT              = 0b010
ACK_FAULT             = 0b100
ACK_NO_ACK            = 0b111

ERR_PROTOCOL          = 1 << 3
ERR_VALUE_MISMATCH    = 1 << 4


# --- Response status codes (single-byte responses) -----------------

DAP_OK                = 0x00
DAP_ERROR             = 0xFF


# --- SWJ_Pins masks ------------------------------------------------

SWJ_PIN_SWCLK_TCK     = 1 << 0
SWJ_PIN_SWDIO_TMS     = 1 << 1
SWJ_PIN_TDI           = 1 << 2
SWJ_PIN_TDO           = 1 << 3
SWJ_PIN_NTRST         = 1 << 5
SWJ_PIN_NRESET        = 1 << 7


class CmsisDapError(Exception):
    """Adapter-level error (non-OK status, malformed response, …)."""


class CmsisDapTransferError(CmsisDapError):
    """A DAP_Transfer reported a non-OK ACK or an error flag.

    ``ack`` is the 3-bit ACK field; ``flags`` carries the
    PROTOCOL/MISMATCH bits."""

    def __init__(self, message: str, ack: int, flags: int = 0):
        super().__init__(message)
        self.ack = ack
        self.flags = flags
