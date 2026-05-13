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

# Sanity bounds applied to whatever we read from the candidate
# control block. They're generous — the goal is "this can't be
# real RTT" detection, not "this matches my exact firmware".
RTT_MAX_BUFFERS = 32
RTT_MAX_BUFFER_SIZE = 64 * 1024


class RttError(Exception):
    """Raised when the control block can't be found, fails sanity
    validation, or the requested channel doesn't exist."""


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

    # Defaults tuned for live-with-GDB use. 100 ms poll is invisible
    # for human-paced log output and keeps bus pressure modest when
    # GDB is also reading memory through the same AP. Override with
    # `poll=N` (ms) when latency matters more than headroom.
    DEFAULT_POLL_MS = 100
    SCAN_CHUNK = 4096
    RESCAN_INTERVAL = 1.0   # seconds between scan retries when not yet found
    # Period between magic-still-present checks while the pump is
    # running. Less aggressive than the buffer poll — its job is to
    # detect a firmware reload (GDB-driven flash, hard reset, …)
    # that wiped the previous control block.
    REVALIDATE_INTERVAL = 1.0

    def __init__(self, ram: Ram):
        super().__init__("rtt")
        self.ram = ram
        # User-tunables. Defaults to channel 0 / 0 — the common case.
        self.up_channel = 0
        self.down_channel = 0
        self.cb_addr: int | None = None
        # User-supplied address (via `address=` option) — preserved
        # so we can fall back to it after an invalidation. None means
        # "scan from scratch".
        self.__fixed_cb_addr: int | None = None
        self.poll_period = self.DEFAULT_POLL_MS / 1000

        # Resolved when the control block is first found (may happen
        # immediately in start() or later via the background rescan).
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
        self.__ready = asyncio.Event()
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
            self.__fixed_cb_addr = self.cb_addr
            return
        if key == "poll":
            self.poll_period = int(value, 0) / 1000
            return
        super().option_set(key, value)

    async def start(self):
        """Start the pump task. The task handles three phases:

        1. Find the control block — either honour an explicit
           `address=` option, or scan the parent Ram. If the scan
           fails, retry every RESCAN_INTERVAL seconds rather than
           failing the start. This is the case where the firmware
           hasn't run yet, or hasn't called `SEGGER_RTT_Init`.

        2. Resolve descriptors — read header + UP / DOWN buffer
           descriptors, cache the offsets, signal `__ready`.

        3. Poll loop — pull bytes from UP into the local read queue.

        Means `serial-server` can attach before the firmware is
        ready; the TCP socket stays open and bytes start flowing
        once RTT initialises."""
        self.__pump_task = asyncio.create_task(self.__main_loop())

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
        until at least one byte is available. Raises RttError if
        the target advertises no UP buffer (MaxNumUpBuffers == 0)."""
        await self.__ready.wait()
        if self.__up_buf_size == 0:
            raise RttError("target has no UP buffer (MaxNumUpBuffers=0)")
        while not self.__rx_queue:
            self.__rx_wakeup.clear()
            await self.__rx_wakeup.wait()
        n = min(size, len(self.__rx_queue))
        out = bytes(self.__rx_queue[:n])
        del self.__rx_queue[:n]
        return out

    async def write(self, data: bytes) -> None:
        """Push `data` into the DOWN ring buffer. Blocks while the
        buffer is full (polls the target RdOff between attempts).

        Also blocks until the control block has been found — for
        firmware that hasn't initialised RTT yet, writes pile up
        client-side rather than disappearing.

        Raises RttError if the target advertises no DOWN buffer
        (MaxNumDownBuffers == 0)."""
        if not data:
            return
        await self.__ready.wait()
        if self.__down_buf_size == 0:
            raise RttError("target has no DOWN buffer (MaxNumDownBuffers=0)")
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
        """Validate the candidate control block end-to-end and
        cache the per-channel ring-buffer state.

        Validation refuses anything that doesn't look like a real
        RTT control block: missing magic, MaxNumUp/Down out of
        sanity range, ring-buffer pointers outside the parent
        Ram, buffer sizes absurd, head/tail offsets past the end
        of their ring. Raises RttError on any of these — the
        outer loop logs and retries (scan again or revisit the
        user-fixed address)."""
        header = await self.ram.bus.mem_read(self.cb_addr, RTT_HEADER_LEN)
        if bytes(header[:RTT_MAGIC_LEN]) != RTT_MAGIC:
            raise RttError(
                f"control block at 0x{self.cb_addr:08x}: magic "
                f"mismatch (read: {bytes(header[:RTT_MAGIC_LEN]).hex()})")
        max_up, max_down = struct.unpack(
            "<II", header[RTT_MAGIC_LEN:RTT_HEADER_LEN])
        # MaxNum*Buffers == 0 is legal: firmware that only logs has
        # no DOWN channel; firmware that only takes commands has no
        # UP. Refuse only nonsense (negative not possible since
        # unsigned, just cap upper bound).
        if max_up > RTT_MAX_BUFFERS:
            raise RttError(
                f"MaxNumUpBuffers={max_up} out of sanity range "
                f"(0..{RTT_MAX_BUFFERS})")
        if max_down > RTT_MAX_BUFFERS:
            raise RttError(
                f"MaxNumDownBuffers={max_down} out of sanity range "
                f"(0..{RTT_MAX_BUFFERS})")
        # A requested channel must exist. Defaulted-to-0 against a
        # max=0 side is accepted as "this direction is absent" —
        # read()/write() will raise if used.
        if max_up > 0 and not (0 <= self.up_channel < max_up):
            raise RttError(
                f"UP channel {self.up_channel} out of range "
                f"(target advertises {max_up} UP buffers)")
        if max_down > 0 and not (0 <= self.down_channel < max_down):
            raise RttError(
                f"DOWN channel {self.down_channel} out of range "
                f"(target advertises {max_down} DOWN buffers)")

        sides = []
        if max_up > 0:
            up_desc_addr = (self.cb_addr + RTT_HEADER_LEN
                            + self.up_channel * RTT_BUFFER_DESC_LEN)
            up = await self.ram.bus.mem_read(
                up_desc_addr, RTT_BUFFER_DESC_LEN)
            _u_name, u_buf, u_size, u_wr, u_rd, _ = struct.unpack("<6I", up)
            sides.append(("UP", up_desc_addr, u_buf, u_size, u_wr, u_rd))
        else:
            up_desc_addr = None
            u_buf = u_size = u_wr = u_rd = 0

        if max_down > 0:
            down_desc_addr = (self.cb_addr + RTT_HEADER_LEN
                              + max_up * RTT_BUFFER_DESC_LEN
                              + self.down_channel * RTT_BUFFER_DESC_LEN)
            down = await self.ram.bus.mem_read(
                down_desc_addr, RTT_BUFFER_DESC_LEN)
            _d_name, d_buf, d_size, d_wr, d_rd, _ = struct.unpack("<6I", down)
            sides.append(("DOWN", down_desc_addr, d_buf, d_size, d_wr, d_rd))
        else:
            down_desc_addr = None
            d_buf = d_size = d_wr = d_rd = 0

        for label, _desc, buf, size, wr, rd in sides:
            if not (0 < size <= RTT_MAX_BUFFER_SIZE):
                raise RttError(
                    f"{label} buffer size {size} out of sanity range "
                    f"(1..{RTT_MAX_BUFFER_SIZE})")
            if not (self.ram.address <= buf < self.ram.end):
                raise RttError(
                    f"{label} buffer @ 0x{buf:08x} is outside parent "
                    f"Ram 0x{self.ram.address:08x}-0x{self.ram.end:08x}")
            if buf + size > self.ram.end:
                raise RttError(
                    f"{label} buffer @ 0x{buf:08x}+{size} extends past "
                    f"parent Ram end 0x{self.ram.end:08x}")
            if wr >= size or rd >= size:
                raise RttError(
                    f"{label} offsets WrOff={wr} RdOff={rd} past "
                    f"buffer size {size}")

        self.__up_buf_addr = u_buf
        self.__up_buf_size = u_size
        self.__up_buf_rdoff = u_rd
        # RdOff sits at offset 16 in the descriptor.
        self.__up_desc_rdoff_addr = (
            up_desc_addr + 16 if up_desc_addr is not None else 0)

        self.__down_buf_addr = d_buf
        self.__down_buf_size = d_size
        self.__down_buf_wroff = d_wr
        self.__down_desc_wroff_addr = (
            down_desc_addr + 12 if down_desc_addr is not None else 0)
        self.__down_desc_rdoff_addr = (
            down_desc_addr + 16 if down_desc_addr is not None else 0)

        up_info = (f"UP[{self.up_channel}]: {u_size}-byte @ 0x{u_buf:08x}"
                   if max_up > 0 else "UP: absent")
        down_info = (
            f"DOWN[{self.down_channel}]: {d_size}-byte @ 0x{d_buf:08x}"
            if max_down > 0 else "DOWN: absent")
        self.logger.info("%s; %s", up_info, down_info)

    async def __check_magic(self):
        """Cheap recheck: is the 16-byte magic still there? If the
        firmware was reloaded (GDB `load`, hardware reset) the old
        control block is gone — we need to re-establish."""
        magic = await self.ram.bus.mem_read(self.cb_addr, RTT_MAGIC_LEN)
        if bytes(magic) != RTT_MAGIC:
            raise RttError(
                f"control block at 0x{self.cb_addr:08x} no longer "
                f"holds the SEGGER magic — firmware likely reloaded")

    # -- Pump main loop ----------------------------------------------

    async def __main_loop(self):
        """Three-state pump:

        ESTABLISH — find a candidate cb_addr (scan or fixed),
            validate everything, cache descriptors. Loops on
            failure with RESCAN_INTERVAL sleeps.
        READY → POLL — set the event; drain UP every poll_period.
            Every REVALIDATE_INTERVAL also re-check that the
            magic is still at cb_addr (cheap 16-byte read).
        On revalidate failure, clear the event, reset cb_addr to
        the user's fixed value (or None for scan), loop back to
        ESTABLISH."""
        try:
            while True:
                await self.__establish_cb()
                self.__ready.set()
                last_revalidate = asyncio.get_event_loop().time()
                while True:
                    if self.__up_buf_size > 0:
                        try:
                            await self.__drain_up_once()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            # Transient bus errors do happen — log
                            # and retry next tick.
                            self.logger.debug(
                                "RTT UP-poll dropped: %s: %s",
                                type(exc).__name__, exc)

                    now = asyncio.get_event_loop().time()
                    if now - last_revalidate >= self.REVALIDATE_INTERVAL:
                        try:
                            await self.__check_magic()
                            last_revalidate = now
                        except RttError as exc:
                            self.logger.info(
                                "RTT control block invalidated: %s — "
                                "re-establishing", exc)
                            self.__ready.clear()
                            self.cb_addr = self.__fixed_cb_addr
                            break  # back to ESTABLISH

                    await asyncio.sleep(self.poll_period)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("RTT pump crashed")
            raise

    async def __establish_cb(self):
        """Find a valid control block. Loops scan-or-revisit until
        validation passes — covers both 'firmware hasn't run yet'
        and 'fixed address turned out to be wrong'."""
        while True:
            if self.cb_addr is None:
                try:
                    self.cb_addr = await self.__scan()
                except RttError as exc:
                    self.logger.info(
                        "%s — retrying in %.1f s "
                        "(firmware may not have called "
                        "SEGGER_RTT_Init yet)",
                        exc, self.RESCAN_INTERVAL)
                    await asyncio.sleep(self.RESCAN_INTERVAL)
                    continue
            try:
                await self.__resolve_descriptors()
                return
            except RttError as exc:
                self.logger.info(
                    "RTT control block at 0x%08x rejected: %s — "
                    "retrying in %.1f s",
                    self.cb_addr, exc, self.RESCAN_INTERVAL)
                # If the user pinned an address, revisit it; else
                # null out so the next iteration scans afresh.
                self.cb_addr = self.__fixed_cb_addr
                await asyncio.sleep(self.RESCAN_INTERVAL)

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
