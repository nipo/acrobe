"""ARM Debug Port (DP).

Provides AP read/write/run commands that cascade through the DP to the
underlying protocol (SWD or JTAG).

SwDp handles SWD-specific pipelining: AP reads are deferred by one SWD
transaction, requiring RDBUFF reads to capture data.
"""

from __future__ import annotations

import asyncio

from ...engine import Batcher
from ...node import Node
from ...protocol import swd


# --- DP command objects ---

class ApRead:
    """Read an AP register through the DP."""

    def __init__(self, ap: int, addr: int):
        self.ap = ap
        self.addr = addr
        self.data = None

    def __repr__(self):
        return f"<ApRead ap={self.ap} addr={self.addr:#x}>"


class ApWrite:
    """Write an AP register through the DP."""

    def __init__(self, ap: int, addr: int, data: int):
        self.ap = ap
        self.addr = addr
        self.data = data

    def __repr__(self):
        return f"<ApWrite ap={self.ap} addr={self.addr:#x} data={self.data:#010x}>"


class DpRead:
    """Read a DP register."""

    def __init__(self, addr: int):
        self.addr = addr
        self.data = None

    def __repr__(self):
        return f"<DpRead addr={self.addr:#x}>"


class DpWrite:
    """Write a DP register."""

    def __init__(self, addr: int, data: int):
        self.addr = addr
        self.data = data

    def __repr__(self):
        return f"<DpWrite addr={self.addr:#x} data={self.data:#010x}>"


class Run:
    """Idle clock cycles."""

    def __init__(self, cycles: int):
        self.cycles = cycles

    def __repr__(self):
        return f"<DpRun cycles={self.cycles}>"


# --- DP Exceptions ---

class DpAccessFailure(Exception):
    """DP access failed (bad ACK, parity error, etc.)."""
    pass


# --- SWD Debug Port ---

class SwDp(Batcher, Node):
    """SWD Debug Port.

    Translates AP/DP commands into SWD Read/Write operations,
    handling SELECT register tracking and SWD read pipelining.
    """

    # DP register addresses (SWD encoding: addr bits [3:2])
    IDCODE = 0x00
    ABORT = 0x00
    CTRL_STAT = 0x04
    SELECT = 0x08
    RDBUFF = 0x0c

    # CTRL/STAT bits
    ORUNDETECT = 1 << 0
    CDBGPWRUPREQ = 1 << 28
    CDBGPWRUPACK = 1 << 29
    CSYSPWRUPREQ = 1 << 30
    CSYSPWRUPACK = 1 << 31

    def __init__(self, interface, name: str = "dp"):
        Batcher.__init__(self)
        Node.__init__(self, name)
        self._interface = interface
        self._select = None  # tracked SELECT register value

    async def start(self):
        """Initialize DP: power up debug and system domains."""
        # Read IDCODE to verify connectivity
        idcode = await self._dp_read(self.IDCODE)
        self.logger.info("DP IDCODE: 0x%08x", idcode)

        # Power up debug and system
        await self._dp_write(self.CTRL_STAT,
                             self.CDBGPWRUPREQ | self.CSYSPWRUPREQ)

        # Poll until both power-up ACKs
        for _ in range(100):
            stat = await self._dp_read(self.CTRL_STAT)
            if (stat & (self.CDBGPWRUPACK | self.CSYSPWRUPACK) ==
                    self.CDBGPWRUPACK | self.CSYSPWRUPACK):
                break
        else:
            raise DpAccessFailure("DP power-up timeout")

        # Enable overrun detection
        await self._dp_write(self.CTRL_STAT,
                             self.CDBGPWRUPREQ | self.CSYSPWRUPREQ |
                             self.ORUNDETECT)

    async def _dp_read(self, addr: int) -> int:
        """Direct DP register read (not batched)."""
        f = self._interface.post(swd.Read(ap=False, addr=addr))
        op = await f
        return op.data

    async def _dp_write(self, addr: int, data: int):
        """Direct DP register write (not batched)."""
        f = self._interface.post(swd.Write(ap=False, addr=addr, data=data))
        await f

    async def flush_ops(self, batch):
        """Translate AP/DP commands into SWD operations.

        Handles SELECT tracking and SWD read pipelining:
        - AP reads are deferred: data arrives on the NEXT SWD read
        - RDBUFF reads are inserted to capture final deferred data
        - SELECT is updated when AP index or register bank changes
        """
        swd_futures = []
        # Tracks (swd_future_that_captures_data, dp_future_to_resolve)
        data_captures = []
        # pending = (dp_op, dp_future) for deferred AP read
        pending = None

        for op, future in batch:
            if isinstance(op, ApRead):
                new_select = (op.ap << 24) | (op.addr & 0xf0)
                self._update_select(new_select, swd_futures)

                swd_f = self._interface.post(
                    swd.Read(ap=True, addr=op.addr & 0x0c))
                swd_futures.append(swd_f)

                if pending is not None:
                    # This read captures the previous pending read's data
                    data_captures.append((swd_f, pending[1]))

                pending = (op, future)

            elif isinstance(op, ApWrite):
                if pending is not None:
                    rdbuff_f = self._interface.post(
                        swd.Read(ap=False, addr=self.RDBUFF))
                    swd_futures.append(rdbuff_f)
                    data_captures.append((rdbuff_f, pending[1]))
                    pending = None

                new_select = (op.ap << 24) | (op.addr & 0xf0)
                self._update_select(new_select, swd_futures)

                swd_f = self._interface.post(
                    swd.Write(ap=True, addr=op.addr & 0x0c, data=op.data))
                swd_futures.append(swd_f)
                future.set_result(None)

            elif isinstance(op, DpRead):
                if pending is not None:
                    rdbuff_f = self._interface.post(
                        swd.Read(ap=False, addr=self.RDBUFF))
                    swd_futures.append(rdbuff_f)
                    data_captures.append((rdbuff_f, pending[1]))
                    pending = None

                swd_f = self._interface.post(
                    swd.Read(ap=False, addr=op.addr))
                swd_futures.append(swd_f)
                data_captures.append((swd_f, future))

            elif isinstance(op, DpWrite):
                if pending is not None:
                    rdbuff_f = self._interface.post(
                        swd.Read(ap=False, addr=self.RDBUFF))
                    swd_futures.append(rdbuff_f)
                    data_captures.append((rdbuff_f, pending[1]))
                    pending = None

                swd_f = self._interface.post(
                    swd.Write(ap=False, addr=op.addr, data=op.data))
                swd_futures.append(swd_f)
                future.set_result(None)

            elif isinstance(op, Run):
                if pending is not None:
                    rdbuff_f = self._interface.post(
                        swd.Read(ap=False, addr=self.RDBUFF))
                    swd_futures.append(rdbuff_f)
                    data_captures.append((rdbuff_f, pending[1]))
                    pending = None

                swd_f = self._interface.post(swd.Run(op.cycles))
                swd_futures.append(swd_f)
                future.set_result(None)

        # Final flush of pending AP read
        if pending is not None:
            rdbuff_f = self._interface.post(
                swd.Read(ap=False, addr=self.RDBUFF))
            swd_futures.append(rdbuff_f)
            data_captures.append((rdbuff_f, pending[1]))

        # Await all SWD operations
        if swd_futures:
            await asyncio.gather(*swd_futures)

        # Resolve reads
        for swd_f, dp_future in data_captures:
            swd_op = swd_f.result()
            dp_future.set_result(swd_op.data)

    def _update_select(self, new_select, swd_futures):
        """Insert a SELECT write if the value changed."""
        if self._select != new_select:
            swd_f = self._interface.post(
                swd.Write(ap=False, addr=self.SELECT, data=new_select))
            swd_futures.append(swd_f)
            self._select = new_select

    def __repr__(self):
        return f"<SwDp {self._name}>"
