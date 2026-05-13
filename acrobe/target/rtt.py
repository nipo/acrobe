"""SEGGER Real-Time Transfer (RTT).

A control block somewhere in target RAM holds two arrays of ring
buffers — UP buffers (target → host) and DOWN buffers (host →
target). The host scans RAM for the literal magic
"SEGGER RTT\\0\\0\\0\\0\\0\\0" (16 bytes, 4-byte aligned) to find
the control block, then operates the ring buffers by polling
target counters between reads.

This file gives one `Rtt` SerialPort per (up, down) channel pair.
Spawned on-demand under a `Ram` region — typically the SoC's SRAM.
The parent Ram supplies bounds for the scan and a working memory
bus. Options:

  rtt(up=N, down=M, address=0xADDR, poll=MS)

`address=` skips the scan. `poll=` overrides the default poll
period (20 ms — fast enough for human-paced logs, slow enough not
to drown the DAP).
"""

from __future__ import annotations

import asyncio
import struct

from ..protocol.serial import SerialConfig, SerialPort, Signals
from .region import Ram


RTT_MAGIC = b"SEGGER RTT\x00\x00\x00\x00\x00\x00"
RTT_MAGIC_LEN = len(RTT_MAGIC)
RTT_HEADER_LEN = RTT_MAGIC_LEN + 8       # magic + max_up + max_down
RTT_BUFFER_DESC_LEN = 24                  # 6 × uint32


class RttError(Exception):
    """Raised when the control block can't be found or the requested
    channel doesn't exist."""


@Ram.db.register("rtt")
class Rtt(SerialPort):
    """SEGGER RTT bidirectional pipe (one UP, one DOWN channel pair).

    Spawned by `Ram.child_spawn("rtt(...)")`. The constructor takes
    the parent Ram; options are applied via `option_set` before
    `start()`. `start()` either uses the configured `address` or
    scans the parent Ram for the magic; then resolves the requested
    channel descriptors; then launches an UP-pump background task
    that polls WrOff and pulls new bytes into a local queue.

    `read(size)` returns at most `size` bytes from the queue,
    blocking until at least one byte is available. `write(data)`
    pushes to the DOWN ring buffer, blocking when the buffer is
    full (per blocking flag — the simple form blocks; future
    flag-aware variant could drop/skip)."""

    DEFAULT_POLL_MS = 20
    SCAN_CHUNK = 4096

    def __init__(self, ram: Ram):
        super().__init__("rtt")
        self.ram = ram
        # User-tunables. Defaults to channel 0 / 0 — the common case.
        self.up_channel = 0
        self.down_channel = 0
        self.cb_addr: int | None = None
        self.poll_period = self.DEFAULT_POLL_MS / 1000

        # Resolved at start():
        self.__up_buf_addr = 0
        self.__up_buf_size = 0
        self.__up_buf_rdoff = 0
        self.__up_desc_rdoff_addr = 0
        self.__down_buf_addr = 0
        self.__down_buf_size = 0
        self.__down_buf_wroff = 0
        self.__down_desc_rdoff_addr = 0
        self.__down_desc_wroff_addr = 0

        self.__rx_queue: bytearray = bytearray()
        self.__rx_wakeup = asyncio.Event()
        self.__pump_task: asyncio.Task | None = None

    # -- Option / lifecycle -----------------------------------------

    def option_set(self, key, value):
        if key in ("up", "rx", "rx_channel"):
            self.up_channel = int(value, 0)
            return
        if key in ("down", "tx", "tx_channel"):
            self.down_channel = int(value, 0)
            return
        if key in ("address", "addr"):
            self.cb_addr = int(value, 0)
            return
        if key == "poll":
            self.poll_period = int(value, 0) / 1000
            return
        super().option_set(key, value)

    async def start(self):
        if self.cb_addr is None:
            self.cb_addr = await self.__scan()
        await self.__resolve_descriptors()
        self.__pump_task = asyncio.create_task(self.__pump_up())

    async def stop(self):
        if self.__pump_task is not None:
            self.__pump_task.cancel()
            try:
                await self.__pump_task
            except asyncio.CancelledError:
                pass
            self.__pump_task = None

    # -- SerialPort surface ------------------------------------------

    async def read(self, size: int) -> bytes:
        """Return up to `size` bytes from the UP channel. Blocks
        until at least one byte is available."""
        while not self.__rx_queue:
            self.__rx_wakeup.clear()
            await self.__rx_wakeup.wait()
        n = min(size, len(self.__rx_queue))
        out = bytes(self.__rx_queue[:n])
        del self.__rx_queue[:n]
        return out

    async def write(self, data: bytes) -> None:
        """Push `data` into the DOWN ring buffer. Blocks while the
        buffer is full (polls the target RdOff between attempts)."""
        if not data:
            return
        remaining = bytes(data)
        while remaining:
            free = await self.__down_free_space()
            if free == 0:
                await asyncio.sleep(self.poll_period)
                continue
            chunk = remaining[:free]
            await self.__down_push(chunk)
            remaining = remaining[len(chunk):]

    # SerialPort line-config / control: stubs. RTT has no UART
    # underneath, so the settings are meaningless — but matching the
    # interface lets the existing `serial-server` CLI work without
    # special-casing.

    async def config_set(self, cfg: SerialConfig) -> SerialConfig:
        return cfg

    async def config_get(self) -> SerialConfig:
        return SerialConfig()

    async def break_set(self, on: bool) -> None:
        pass

    async def dtr_set(self, on: bool) -> None:
        pass

    async def rts_set(self, on: bool) -> None:
        pass

    async def signals_get(self) -> Signals:
        return Signals()

    async def flush(self, tx: bool = True, rx: bool = True) -> None:
        if rx:
            self.__rx_queue.clear()
        # TX flush is implicit — write() doesn't return until bytes
        # are in the target's ring buffer.

    # -- Discovery + descriptor setup --------------------------------

    async def __scan(self) -> int:
        """Walk the parent Ram in chunks looking for the magic.

        Chunks overlap by `RTT_MAGIC_LEN - 1` bytes so the magic
        can't straddle a chunk boundary unobserved. The scan is
        word-aligned — SEGGER's control block is 4-byte aligned by
        construction."""
        addr = self.ram.address
        end = self.ram.end
        self.logger.info(
            "Scanning %s (0x%08x-0x%08x, %d bytes) for SEGGER RTT…",
            self.ram.name, addr, end, end - addr)
        cursor = addr
        while True:
            chunk_end = min(cursor + self.SCAN_CHUNK, end)
            if chunk_end - cursor < RTT_MAGIC_LEN:
                break
            chunk = await self.ram.read(cursor - self.ram.address,
                                         chunk_end - cursor)
            for offset in range(0, len(chunk) - RTT_MAGIC_LEN + 1, 4):
                if chunk[offset:offset + RTT_MAGIC_LEN] == RTT_MAGIC:
                    cb = cursor + offset
                    self.logger.info("Found RTT control block at 0x%08x",
                                     cb)
                    return cb
            if chunk_end == end:
                break
            cursor = (chunk_end - (RTT_MAGIC_LEN - 1) + 3) & ~3
        raise RttError(f"SEGGER RTT magic not found in {self.ram.name}")

    async def __resolve_descriptors(self):
        """Read the header + the two descriptors we care about and
        cache the addresses / sizes / offsets for the pump."""
        header = await self.ram.bus.mem_read(self.cb_addr, RTT_HEADER_LEN)
        max_up, max_down = struct.unpack(
            "<II", header[RTT_MAGIC_LEN:RTT_HEADER_LEN])
        if not (0 <= self.up_channel < max_up):
            raise RttError(
                f"UP channel {self.up_channel} out of range "
                f"(target advertises {max_up} UP buffers)")
        if not (0 <= self.down_channel < max_down):
            raise RttError(
                f"DOWN channel {self.down_channel} out of range "
                f"(target advertises {max_down} DOWN buffers)")

        up_desc_addr = (self.cb_addr + RTT_HEADER_LEN
                        + self.up_channel * RTT_BUFFER_DESC_LEN)
        down_desc_addr = (self.cb_addr + RTT_HEADER_LEN
                          + max_up * RTT_BUFFER_DESC_LEN
                          + self.down_channel * RTT_BUFFER_DESC_LEN)

        up = await self.ram.bus.mem_read(up_desc_addr, RTT_BUFFER_DESC_LEN)
        down = await self.ram.bus.mem_read(down_desc_addr, RTT_BUFFER_DESC_LEN)
        u_name, u_buf, u_size, u_wr, u_rd, _ = struct.unpack("<6I", up)
        d_name, d_buf, d_size, d_wr, d_rd, _ = struct.unpack("<6I", down)

        self.__up_buf_addr = u_buf
        self.__up_buf_size = u_size
        self.__up_buf_rdoff = u_rd
        # RdOff sits at offset 16 in the descriptor.
        self.__up_desc_rdoff_addr = up_desc_addr + 16

        self.__down_buf_addr = d_buf
        self.__down_buf_size = d_size
        self.__down_buf_wroff = d_wr
        self.__down_desc_wroff_addr = down_desc_addr + 12
        self.__down_desc_rdoff_addr = down_desc_addr + 16

        self.logger.info(
            "UP[%d]: %d-byte buffer @ 0x%08x; DOWN[%d]: %d-byte @ 0x%08x",
            self.up_channel, u_size, u_buf,
            self.down_channel, d_size, d_buf)

    # -- UP pump ----------------------------------------------------

    async def __pump_up(self):
        try:
            while True:
                await self.__drain_up_once()
                await asyncio.sleep(self.poll_period)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("RTT UP pump crashed")
            raise

    async def __drain_up_once(self):
        # Target writes WrOff at descriptor offset 12.
        wroff_bytes = await self.ram.bus.mem_read(
            self.__up_desc_rdoff_addr - 4, 4)
        wroff = struct.unpack("<I", wroff_bytes)[0]
        if wroff == self.__up_buf_rdoff:
            return
        size = self.__up_buf_size
        rd = self.__up_buf_rdoff
        if wroff > rd:
            data = await self.ram.bus.mem_read(
                self.__up_buf_addr + rd, wroff - rd)
        else:
            # Wrap: two reads.
            tail = await self.ram.bus.mem_read(
                self.__up_buf_addr + rd, size - rd)
            head = await self.ram.bus.mem_read(
                self.__up_buf_addr, wroff)
            data = tail + head
        self.__rx_queue.extend(data)
        self.__up_buf_rdoff = wroff
        await self.ram.bus.mem_write(
            self.__up_desc_rdoff_addr,
            struct.pack("<I", wroff))
        if data:
            self.__rx_wakeup.set()

    # -- DOWN write -------------------------------------------------

    async def __down_free_space(self) -> int:
        """How many bytes we can push before the DOWN buffer is full.

        Ring-buffer fullness rule: leave one slot empty to keep
        WrOff != RdOff distinguishable from "completely full"."""
        rdoff_bytes = await self.ram.bus.mem_read(
            self.__down_desc_rdoff_addr, 4)
        rdoff = struct.unpack("<I", rdoff_bytes)[0]
        size = self.__down_buf_size
        wr = self.__down_buf_wroff
        if rdoff > wr:
            return rdoff - wr - 1
        return size - wr + rdoff - 1

    async def __down_push(self, data: bytes):
        size = self.__down_buf_size
        wr = self.__down_buf_wroff
        if wr + len(data) <= size:
            await self.ram.bus.mem_write(self.__down_buf_addr + wr, data)
        else:
            # Wrap: two writes.
            first = size - wr
            await self.ram.bus.mem_write(
                self.__down_buf_addr + wr, data[:first])
            await self.ram.bus.mem_write(
                self.__down_buf_addr, data[first:])
        new_wr = (wr + len(data)) % size
        self.__down_buf_wroff = new_wr
        await self.ram.bus.mem_write(
            self.__down_desc_wroff_addr,
            struct.pack("<I", new_wr))
