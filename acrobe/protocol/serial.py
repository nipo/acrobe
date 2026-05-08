"""Serial port abstraction — a Pipe superset with line configuration."""

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace

from ..node import Node


class Parity(enum.Enum):
    NONE = "none"
    EVEN = "even"
    ODD = "odd"
    MARK = "mark"
    SPACE = "space"


class StopBits(enum.Enum):
    ONE = 1
    ONE_AND_HALF = 1.5
    TWO = 2


class FlowControl(enum.Enum):
    NONE = "none"
    XON_XOFF = "xon_xoff"
    RTS_CTS = "rts_cts"
    DSR_DTR = "dsr_dtr"


@dataclass(frozen=True, slots=True)
class SerialConfig:
    baud: int = 9600
    data_bits: int = 8
    parity: Parity = Parity.NONE
    stop_bits: StopBits = StopBits.ONE
    flow_control: FlowControl = FlowControl.NONE

    def with_(self, **kwargs) -> "SerialConfig":
        return replace(self, **kwargs)


@dataclass(frozen=True, slots=True)
class Signals:
    """Modem status lines (input-to-DTE: driven by the remote end)."""
    cts: bool = False
    dsr: bool = False
    ri: bool = False
    dcd: bool = False


class LineState(enum.IntFlag):
    """Error flags reported by the UART on the received character stream.

    Bit layout matches RFC 2217 NOTIFY-LINESTATE (subset of UART LSR).
    """
    DATA_READY       = 0x01
    OVERRUN_ERROR    = 0x02
    PARITY_ERROR     = 0x04
    FRAMING_ERROR    = 0x08
    BREAK_DETECT     = 0x10
    TX_HOLD_EMPTY    = 0x20
    TX_SHIFT_EMPTY   = 0x40
    TIMEOUT          = 0x80


def signals_to_modemstate(sig: Signals) -> int:
    """Encode Signals as an RFC 2217 modem state byte (upper nibble)."""
    v = 0
    if sig.cts: v |= 0x10
    if sig.dsr: v |= 0x20
    if sig.ri:  v |= 0x40
    if sig.dcd: v |= 0x80
    return v


def modemstate_to_signals(b: int) -> Signals:
    return Signals(
        cts=bool(b & 0x10),
        dsr=bool(b & 0x20),
        ri=bool(b & 0x40),
        dcd=bool(b & 0x80),
    )


class SerialPort(Node, ABC):
    """Abstract async serial port.

    Superset of Pipe: adds line config, break, flush, modem signals,
    and registrable callbacks for signal edges and line-state errors.

    All I/O is asynchronous. read(size) returns exactly size bytes
    (matches Pipe contract).
    """

    def __init__(self, name: str):
        super().__init__(name)
        self._on_signals_cbs = []
        self._on_linestate_cbs = []

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def read(self, size: int) -> bytes: ...

    @abstractmethod
    async def config_set(self, cfg: SerialConfig) -> SerialConfig:
        """Apply cfg. Returns the configuration actually applied
        (may differ — e.g. baud clamped to hardware)."""

    @abstractmethod
    async def config_get(self) -> SerialConfig: ...

    @abstractmethod
    async def break_set(self, on: bool) -> None: ...

    @abstractmethod
    async def dtr_set(self, on: bool) -> None: ...

    @abstractmethod
    async def rts_set(self, on: bool) -> None: ...

    @abstractmethod
    async def signals_get(self) -> Signals: ...

    @abstractmethod
    async def flush(self, tx: bool = True, rx: bool = True) -> None: ...

    def on_signals(self, cb):
        """Register a callback invoked on modem-line edge.

        cb signature: cb(old: Signals, new: Signals)
        """
        self._on_signals_cbs.append(cb)

    def on_linestate(self, cb):
        """Register a callback invoked on UART line-state change.

        cb signature: cb(flags: LineState)
        """
        self._on_linestate_cbs.append(cb)

    def _emit_signals(self, old: Signals, new: Signals):
        for cb in list(self._on_signals_cbs):
            try:
                cb(old, new)
            except Exception:
                self.logger.exception("signals callback raised")

    def _emit_linestate(self, flags: "LineState"):
        for cb in list(self._on_linestate_cbs):
            try:
                cb(flags)
            except Exception:
                self.logger.exception("linestate callback raised")
