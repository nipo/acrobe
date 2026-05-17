"""SPI passthrough over PICOBOOT — drives the RP2040's QSPI flash
pins as a generic SPI bus by running a stub on the target.

The stub (`flash_spi_transact`) walks a command array in target RAM
and processes one entry at a time:

- A "CS control" entry (size's high bit set) drives the QSPI CSn
  pin high or low via IO_QSPI bypass overrides — bypassing the SSI
  controller's built-in CS so longer-than-one-transfer transactions
  can be held atomically.
- A "transfer" entry pumps ``size`` bytes through the SSI controller's
  DR0 register in full-duplex: bytes from ``tx_ptr`` go out on D0,
  bytes coming in on D1 are stored to ``rx_ptr``. Either pointer
  may be NULL to skip that direction.

Reference C source: crobe ``firmware/flash/stubs/arm/rp2040.c``.
Compiled bytes (Thumb-1, 124 bytes) reused here verbatim with a
4-byte placeholder (``0xdeadbee0``) the host patches per
transaction to point at the command array. The stub runs through a
:class:`PicobootPuppet` — the puppet uploads it, sets the entry
point, runs it via PICOBOOT EXEC, and the stub returns when it
encounters a null-size command.

Per crobe's experience: ``exit_xip`` is required before the first
transfer because the bootrom leaves SSI configured for XIP reads,
and the stub assumes manual SR-polled transfers.
"""

from __future__ import annotations

import asyncio
import struct

from ...db import NoMatch
from ...protocol import spi
from .picoboot import Picoboot, PicobootPuppet


# Placeholder the stub patches per call. Replaced by the host with
# the address of the command array in target RAM.
PLACEHOLDER = 0xDEADBEE0

# Compiled stub bytes — see crobe firmware/flash/stubs/arm/rp2040.c.
# Verified unique-marker, 124 bytes, ARMv6-M Thumb.
SPI_TRANSACT_STUB = bytes.fromhex(
    "f7b5c0261b4a76059368002b30d008da01210b40023318491b020b600b1c1b68"
    "24e01c1c11685068251c1d431ed0356a776a0197c0277f05bc46002b0ad0019f"
    "7d190d2d06d80d1e01d00d7801316746013b3d66019d002de6d065462d6eedb2"
    "002801d005700130013cdde70c32cbe7f7bdc046e0beadde0c800140")

# Command-entry encoding (12 bytes, little-endian):
#   tx_ptr   uint32   bytes to send (0 = drive zeros, don't read tx)
#   rx_ptr   uint32   buffer for received bytes (0 = discard rx)
#   size     uint32   byte count; high bit set = CS control entry
#                      (bit 0: 0 = drive CS low, 1 = drive CS high)
CMD_CS_LOW  = 0x80000000
CMD_CS_HIGH = 0x80000001


class PicobootSpiInterface(spi.Interface):
    """RP2040 QSPI flash pins as an `spi.Interface`, driven by a
    stub on the chip.

    Constructed by `Rp2040Target.child_spawn("spi")` with the
    Target's puppet — there is exactly one puppet per chip
    lifetime, owned by the Target, and every code path that needs
    to run on-target stubs (SPI passthrough, future SFDP probe,
    Loadable-side helpers) shares that allocator. Constructing
    multiple puppets against the same SRAM would race their
    allocators.

    Adds a single `spi.Target` child named ``cs0`` since RP2040's
    QSPI block has one CSn pin.

    Multi-Shift CS-held transactions become a single stub call:
    Cs(0) + N×Shift + Cs(None) → one packed command array, one
    PICOBOOT EXEC. The host pays one USB round-trip for the whole
    transaction instead of one per shift.
    """

    EXEC_TIMEOUT_S = 30.0

    def __init__(self, picoboot: Picoboot, puppet: PicobootPuppet,
                 name: str = "spi"):
        super().__init__(adapter=None, name=name)
        self.picoboot = picoboot
        self.puppet = puppet
        self.__marker_offset = SPI_TRANSACT_STUB.index(
            PLACEHOLDER.to_bytes(4, "little"))
        self.__stub_zone = None

        self.child_add(spi.Target(self, cs=0, mode=0, name="cs0"))

    async def start(self):
        await self.__setup_ssi()

    async def __setup_ssi(self):
        # Take SSI out of XIP mode and into the manual SR-polled
        # configuration the stub assumes. The bootrom's EXIT_XIP
        # command is idempotent and reconfigures SSI registers
        # (CTRL0, BAUDR, SSIENR), so calling it twice does no
        # harm — we treat it as the defensive opener before any
        # stub call, not a one-shot setup.
        self.logger.protocol("SSI exit_xip + setup")
        await self.picoboot.transport.exit_xip()

    async def __ensure_stub(self):
        if self.__stub_zone is not None:
            return
        self.__stub_zone = self.puppet.allocate(
            len(SPI_TRANSACT_STUB), align=4)
        await self.__stub_zone.write(SPI_TRANSACT_STUB)
        self.logger.protocol(
            "SPI stub installed at 0x%08x (%d bytes)",
            self.__stub_zone.address, len(SPI_TRANSACT_STUB))

    async def stop(self):
        # Return the stub zone to the shared puppet allocator so a
        # later spawn (or other code using the same puppet) can
        # reuse the space.
        if self.__stub_zone is not None:
            self.puppet.unallocate(self.__stub_zone)
            self.__stub_zone = None

    async def flush_ops(self, batch):
        await self.__ensure_stub()
        # Defensive: if a caller posted ops before start() ran (e.g.
        # constructed the interface and called .post() directly),
        # we still want SSI in manual mode before the stub fires.
        if not self.started:
            await self.__setup_ssi()

        # Partition into (kind, op, future) — Cs / Shift only.
        entries: list[tuple[str, object, asyncio.Future]] = []
        for op, future in batch:
            if isinstance(op, spi.Cs) or isinstance(op, spi.Shift):
                entries.append((
                    "cs" if isinstance(op, spi.Cs) else "shift",
                    op, future))
            else:
                future.set_exception(NotImplementedError(
                    f"PicobootSpi: unsupported op {op!r}"))

        if not entries:
            return

        n_entries = len(entries)
        cmd_buf_size = 12 * (n_entries + 1)
        tx_total = sum(op.byte_count for k, op, _ in entries if k == "shift")
        rx_total = sum(op.byte_count for k, op, _ in entries
                       if k == "shift" and op.read_miso)

        zone = self.puppet.allocate(
            cmd_buf_size + tx_total + rx_total, align=4)
        try:
            cmd_buf_addr = zone.address
            tx_base = cmd_buf_addr + cmd_buf_size
            rx_base = tx_base + tx_total

            cmd_bytes, tx_blob, rx_slots = self.__layout(
                entries, tx_base, rx_base)

            # Upload cmd array + tx data in one bulk write.
            await self.puppet.transport.write(
                cmd_buf_addr, bytes(cmd_bytes) + bytes(tx_blob))

            # Patch the stub's placeholder with the cmd-array
            # address. One 4-byte mem_write at a known offset.
            await self.puppet.transport.write(
                self.__stub_zone.address + self.__marker_offset,
                cmd_buf_addr.to_bytes(4, "little"))

            await self.puppet.prepare(self.__stub_zone.address | 1)
            await self.puppet.run()
            await self.puppet.wait(timeout=self.EXEC_TIMEOUT_S)

            if rx_total > 0:
                rx_data = await self.puppet.transport.read(
                    rx_base, rx_total)
            else:
                rx_data = b""

            self.__dispatch_results(entries, rx_slots, rx_base, rx_data)
        finally:
            self.puppet.unallocate(zone)

    @staticmethod
    def __layout(entries, tx_base, rx_base):
        """Build the cmd-array bytes, concatenated tx blob, and a
        list of (op, future, rx_addr, count) for results. ``rx_addr``
        is None for shifts that don't read miso."""
        cmd_entries: list[tuple[int, int, int]] = []
        tx_blob = bytearray()
        rx_slots: list[tuple] = []
        cur_tx = tx_base
        cur_rx = rx_base

        for kind, op, future in entries:
            if kind == "cs":
                cs_word = (
                    CMD_CS_HIGH if op.value is None else CMD_CS_LOW)
                cmd_entries.append((0, 0, cs_word))
                rx_slots.append((op, future, None, 0))
                continue
            # shift
            tx_data = (op.mosi if isinstance(op.mosi, bytes)
                       else bytes(op.mosi))
            this_tx_ptr = cur_tx
            tx_blob.extend(tx_data)
            cur_tx += len(tx_data)
            if op.read_miso:
                this_rx_ptr = cur_rx
                rx_slots.append((op, future, cur_rx, op.byte_count))
                cur_rx += op.byte_count
            else:
                this_rx_ptr = 0
                rx_slots.append((op, future, None, 0))
            cmd_entries.append((this_tx_ptr, this_rx_ptr, op.byte_count))

        # Null-terminator entry (size=0 ends the stub loop).
        cmd_bytes = b"".join(
            struct.pack("<III", a, b, c) for a, b, c in cmd_entries)
        cmd_bytes += bytes(12)
        return cmd_bytes, tx_blob, rx_slots

    @staticmethod
    def __dispatch_results(entries, rx_slots, rx_base, rx_data):
        for (kind, op, future), (_, _, rx_addr, count) in zip(
                entries, rx_slots):
            if kind == "shift" and rx_addr is not None:
                off = rx_addr - rx_base
                op.miso = bytes(rx_data[off:off + count])
            future.set_result(None)
