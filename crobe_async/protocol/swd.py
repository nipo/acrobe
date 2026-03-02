from __future__ import annotations

import asyncio
from enum import IntEnum

from ..bitstring import BitString
from ..engine import Batcher
from ..component import Component


class Ack(IntEnum):
    OK = 1
    WAIT = 2
    ERROR = 4
    PARITY_ERR = 8


# --- SWD Operations ---

class Read:
    """SWD read operation (DP or AP register)."""

    def __init__(self, ap: bool, addr: int):
        self.ap = bool(ap)
        self.addr = addr & 0x0c
        parity = int(self.ap) ^ ((self.addr >> 2) & 1) ^ ((self.addr >> 3) & 1) ^ 1
        self.cmd = (int(self.ap) << 1) | (((self.addr >> 2) & 3) << 3) | (parity << 5) | 0x85
        self.data = None
        self.ack = None

    def __repr__(self):
        return f"<Read {'AP' if self.ap else 'DP'} addr={self.addr:#x}>"


class Write:
    """SWD write operation (DP or AP register)."""

    def __init__(self, ap: bool, addr: int, data: int):
        self.ap = bool(ap)
        self.addr = addr & 0x0c
        self.data = data & 0xffffffff
        parity = int(self.ap) ^ ((self.addr >> 2) & 1) ^ ((self.addr >> 3) & 1)
        self.cmd = (int(self.ap) << 1) | (((self.addr >> 2) & 3) << 3) | (parity << 5) | 0x81
        self.ack = None

    def __repr__(self):
        return f"<Write {'AP' if self.ap else 'DP'} addr={self.addr:#x} data={self.data:#010x}>"


class Run:
    """Run idle cycles (SWCLK with SWDIO low)."""

    def __init__(self, cycles: int):
        self.cycles = cycles

    def __repr__(self):
        return f"<Run cycles={self.cycles}>"


class Wakeup:
    """Wake target from dormant (SWCLK with SWDIO high)."""

    def __init__(self, cycles: int = 50):
        self.cycles = cycles

    def __repr__(self):
        return f"<Wakeup cycles={self.cycles}>"


# --- Protocol switch sequences ---

class JtagToSwd:
    """JTAG-to-SWD switch sequence."""
    tms = BitString(-1, 50) + BitString(0xe73c, 16) + BitString(-1, 5)

    def __repr__(self):
        return "JtagToSwd()"


class SwdToDormant:
    """SWD-to-Dormant switch sequence."""
    tms = BitString(-1, 50) + BitString(0xe3bc, 16)

    def __repr__(self):
        return "SwdToDormant()"


class DormantToSwd:
    """Dormant-to-SWD wake sequence."""
    ALERT = 0x19bc0ea2e3ddafe986852d956209f392
    ACTIVATION = 0x1a
    tms = (BitString(-1, 50) +
           BitString(ALERT, 128) +
           BitString(0, 4) +
           BitString(ACTIVATION, 8))

    def __repr__(self):
        return "DormantToSwd()"


class LineReset:
    """SWD line reset: 50+ SWCLK cycles with SWDIO high, then idle."""
    tms = BitString(-1, 50) + BitString(0, 2)

    def __repr__(self):
        return "LineReset()"


# --- SWD Interface ---

class Interface(Batcher, Component):
    """SWD wire interface. Forwards Read/Write/Run/Wakeup to adapter."""

    def __init__(self, adapter, name="swd"):
        Batcher.__init__(self)
        Component.__init__(self, name)
        self._adapter = adapter

    async def flush_ops(self, batch):
        futures = []
        for op, future in batch:
            futures.append((self._adapter.post(op), future))
        if futures:
            await asyncio.gather(*[f for f, _ in futures])
        for af, mf in futures:
            mf.set_result(af.result())

    def __repr__(self):
        return f"<swd.Interface {self._name}>"


# --- DP register addresses ---

DP_IDCODE = 0x00
DP_ABORT = 0x00     # write-only
DP_CTRL_STAT = 0x04
DP_SELECT = 0x08
DP_RDBUFF = 0x0c
DP_TARGETSEL = 0x0c  # write-only (multidrop)
