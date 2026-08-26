"""RFC 2217 Com Port Control option: subcommand codes and encoders.

Per RFC 2217 §3, the subcommand space is split:
  0x00-0x0C  client-to-server
  0x64-0x70  server-to-client ( = client code + 100 )

A server echoes most requests back using the shifted code to confirm.
"""

import struct

from ..protocol.serial import (
    Parity, StopBits, FlowControl, SerialConfig, Signals,
)


OPTION_COM_PORT_CONTROL = 44  # RFC 2217

# Client-side subcommand codes (master → slave, aka PC → modem).
SIGNATURE           = 0
SET_BAUDRATE        = 1
SET_DATASIZE        = 2
SET_PARITY          = 3
SET_STOPSIZE        = 4
SET_CONTROL         = 5
NOTIFY_LINESTATE    = 6
NOTIFY_MODEMSTATE   = 7
FLOWCONTROL_SUSPEND = 8
FLOWCONTROL_RESUME  = 9
SET_LINESTATE_MASK  = 10
SET_MODEMSTATE_MASK = 11
PURGE_DATA          = 12

# Server → client shift. A server response to subcommand N uses N+100.
SERVER_SHIFT = 100


# -- SET_PARITY values (client/server use the same table)
PARITY_REQUEST  = 0   # "tell me current" (client-side request only)
PARITY_NONE     = 1
PARITY_ODD      = 2
PARITY_EVEN     = 3
PARITY_MARK     = 4
PARITY_SPACE    = 5

_PARITY_TO_CODE = {
    Parity.NONE:  PARITY_NONE,
    Parity.ODD:   PARITY_ODD,
    Parity.EVEN:  PARITY_EVEN,
    Parity.MARK:  PARITY_MARK,
    Parity.SPACE: PARITY_SPACE,
}
_CODE_TO_PARITY = {v: k for k, v in _PARITY_TO_CODE.items()}


# -- SET_STOPSIZE values
STOPSIZE_REQUEST = 0
STOPSIZE_1       = 1
STOPSIZE_2       = 2
STOPSIZE_1_5     = 3

_STOP_TO_CODE = {
    StopBits.ONE:         STOPSIZE_1,
    StopBits.TWO:         STOPSIZE_2,
    StopBits.ONE_AND_HALF: STOPSIZE_1_5,
}
_CODE_TO_STOP = {v: k for k, v in _STOP_TO_CODE.items()}


# -- SET_CONTROL values (mixes flow-control, break, DTR, RTS)
CONTROL_REQUEST_FLOW     = 0
CONTROL_FLOW_NONE        = 1
CONTROL_FLOW_XONXOFF     = 2
CONTROL_FLOW_RTSCTS      = 3
CONTROL_REQUEST_BREAK    = 4
CONTROL_BREAK_ON         = 5
CONTROL_BREAK_OFF        = 6
CONTROL_REQUEST_DTR      = 7
CONTROL_DTR_ON           = 8
CONTROL_DTR_OFF          = 9
CONTROL_REQUEST_RTS      = 10
CONTROL_RTS_ON           = 11
CONTROL_RTS_OFF          = 12
CONTROL_FLOW_DSRDTR      = 16
# (RFC 2217 doesn't name input/output flow separately in client→server
# direction; we use the "both directions" codes 1-3 / 16.)

_FLOW_TO_CODE = {
    FlowControl.NONE:     CONTROL_FLOW_NONE,
    FlowControl.XON_XOFF: CONTROL_FLOW_XONXOFF,
    FlowControl.RTS_CTS:  CONTROL_FLOW_RTSCTS,
    FlowControl.DSR_DTR:  CONTROL_FLOW_DSRDTR,
}
_CODE_TO_FLOW = {v: k for k, v in _FLOW_TO_CODE.items()}


# -- PURGE_DATA values
PURGE_RX     = 1
PURGE_TX     = 2
PURGE_RX_TX  = 3


# ----------------------------------------------------------------------
# Encoders / decoders for each subcommand's payload.
# Each returns/accepts the bytes that follow the subcommand byte itself
# (i.e. without the leading subcommand code).
# ----------------------------------------------------------------------

def encode_baudrate(baud: int) -> bytes:
    return struct.pack(">I", baud)


def decode_baudrate(payload: bytes) -> int:
    if len(payload) != 4:
        raise ValueError(f"Bad baudrate payload length {len(payload)}")
    return struct.unpack(">I", payload)[0]


def encode_parity(p: Parity) -> bytes:
    return bytes([_PARITY_TO_CODE[p]])


def decode_parity(payload: bytes) -> Parity:
    if len(payload) != 1:
        raise ValueError(f"Bad parity payload length {len(payload)}")
    code = payload[0]
    if code not in _CODE_TO_PARITY:
        raise ValueError(f"Unknown parity code {code}")
    return _CODE_TO_PARITY[code]


def encode_stopbits(s: StopBits) -> bytes:
    return bytes([_STOP_TO_CODE[s]])


def decode_stopbits(payload: bytes) -> StopBits:
    if len(payload) != 1:
        raise ValueError(f"Bad stopbits payload length {len(payload)}")
    code = payload[0]
    if code not in _CODE_TO_STOP:
        raise ValueError(f"Unknown stop-size code {code}")
    return _CODE_TO_STOP[code]


def encode_datasize(bits: int) -> bytes:
    if not 5 <= bits <= 8:
        raise ValueError(f"Unsupported data size {bits}")
    return bytes([bits])


def decode_datasize(payload: bytes) -> int:
    if len(payload) != 1:
        raise ValueError(f"Bad datasize payload length {len(payload)}")
    return payload[0]


def encode_flow(f: FlowControl) -> bytes:
    return bytes([_FLOW_TO_CODE[f]])


def decode_flow(payload: bytes) -> FlowControl:
    if len(payload) != 1:
        raise ValueError(f"Bad flow payload length {len(payload)}")
    code = payload[0]
    if code not in _CODE_TO_FLOW:
        raise ValueError(f"Unknown flow-control code {code}")
    return _CODE_TO_FLOW[code]


def encode_modemstate(sig: Signals, delta: int = 0) -> bytes:
    """Encode a Signals snapshot as a NOTIFY_MODEMSTATE payload.

    delta carries edge flags in the low nibble (we don't track edges
    in Signals, so default 0 — the receiver can compare against its
    own last snapshot if it needs edges).
    """
    from ..protocol.serial import signals_to_modemstate
    return bytes([signals_to_modemstate(sig) | (delta & 0x0f)])


def decode_modemstate(payload: bytes) -> Signals:
    from ..protocol.serial import modemstate_to_signals
    if len(payload) != 1:
        raise ValueError(f"Bad modemstate payload length {len(payload)}")
    return modemstate_to_signals(payload[0])


def encode_linestate(flags: int) -> bytes:
    return bytes([flags & 0xff])


def decode_linestate(payload: bytes) -> int:
    if len(payload) != 1:
        raise ValueError(f"Bad linestate payload length {len(payload)}")
    return payload[0]


def encode_purge(what: int) -> bytes:
    return bytes([what])


def decode_purge(payload: bytes) -> int:
    if len(payload) != 1:
        raise ValueError(f"Bad purge payload length {len(payload)}")
    return payload[0]
