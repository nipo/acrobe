#!/usr/bin/env python3.13
"""Standalone Agilex 5 configuration test.

Programs J2.bin via the SDM CONFIG_DATA/CONFIG_STATUS streaming
protocol, using the SDM command layer for sync and status.

Usage:
    python3.13 config_test.py <adapter-path> <j2bin-path>
    e.g.: python3.13 config_test.py ub3-/jtag /tmp/de25-nano-spi/data/J2.bin
"""

import asyncio
import logging
import sys
import time

from acrobe.adapter.model import HwRoot, UsbEnumerator
from acrobe.bitstring import BitString
from acrobe.protocol.jtag import CaptureIr, CaptureDr, Shift, Run
from acrobe.component.altera.agilex5 import Agilex5, Agilex5SdmCommand
from acrobe.component.altera.sdm_jtag import SdmJtag
from acrobe.component.altera.sdm import SdmError
from acrobe import log


# Bitstream header: 0xA17E2A00 FFFFFFFF (64 bits, little-endian)
STREAM_HEADER = BitString(
    (0xA17E2A00_FFFFFFFF).to_bytes(8, 'little'), 64)
STREAM_TRAILER = BitString(0, 1)

# Chunk sizing
INITIAL_CHUNK = 32768     # bits
MAX_CHUNK = 524288        # bits

# CONFIG_STATUS IR instruction
CONFIG_STATUS_IR = 0x208
CONFIG_DATA_IR = 0x002
STATUS_DR_BITS = 37


async def config_status_shift(tap, request_data=False,
                               start_config=False, enable=False):
    """Shift CONFIG_STATUS (IR 0x208) and return decoded fields.

    TDI bits:
      [0] request_data
      [1] start_config
      [2] enable

    TDO bits:
      [0]     done (SDM busy)
      [1]     error
      [31:2]  progress (words consumed, ×32 = bits)
      [36:32] fifo_free
    """
    tdi_val = int(request_data) | (int(start_config) << 1) | (int(enable) << 2)
    tdi = BitString(tdi_val, STATUS_DR_BITS)

    result = await tap.ir(CONFIG_STATUS_IR, dr_length=STATUS_DR_BITS)(
        tdi, read_tdo=True)
    await tap.run(16)

    val = int(result)
    done = bool(val & 1)
    error = bool(val & 2)
    progress_words = (val >> 2) & 0x3FFFFFFF
    fifo_free = (val >> 32) & 0x1F
    return done, error, progress_words * 32, fifo_free


async def config_data_shift(tap, data_bits, bit_count):
    """Shift CONFIG_DATA (IR 0x002): header + data + trailer."""
    frame = STREAM_HEADER + data_bits + STREAM_TRAILER
    total = len(frame)
    await tap.ir(CONFIG_DATA_IR, dr_length=total)(frame, read_tdo=False)
    await tap.run(16)
    return total


async def stream_bitstream(tap, data: bytes):
    """Stream bitstream data using CONFIG_DATA/CONFIG_STATUS protocol.

    Matches STAPL j127/j125 flow control:
    - First poll: request_data + start_config + enable (TDI=7)
    - After DONE=1 stall: request_data (TDI=1)
    - Normal: no flags (TDI=0)
    - Chunk doubling on success, halving on stall
    - Data position tracks SDM progress counter
    """
    total_bits = len(data) * 8
    chunk_size = INITIAL_CHUNK
    j123 = True  # first-iteration flag (matches STAPL J123)
    j123_active = True  # prevents chunk doubling until first send
    j112 = False  # was-stalled flag (matches STAPL J112)
    retries_left = 6000  # matches STAPL J115

    t0 = time.time()
    sent_count = 0

    while True:
        # Determine TDI flags (matching STAPL j125 setup)
        if j123:
            request_data = True
            start_config = True
            enable = True
        else:
            # request_data when SDM was stalled with no FIFO space
            request_data = (j112 and True)  # simplified: always request after stall
            start_config = False
            enable = False

        done, error, progress, fifo_free = await config_status_shift(
            tap,
            request_data=request_data,
            start_config=start_config,
            enable=enable,
        )

        if j123:
            j123 = False

        if error:
            raise RuntimeError(
                f"SDM error during streaming (progress={progress}/{total_bits})")

        if progress >= total_bits:
            elapsed = time.time() - t0
            print(f"  Streaming complete: {total_bits} bits in {elapsed:.1f}s "
                  f"({total_bits/elapsed/1e6:.1f} Mbit/s), "
                  f"{sent_count} shifts")
            return

        retries_left -= 1
        if retries_left <= 0:
            raise RuntimeError(
                f"SDM streaming timeout (progress={progress}/{total_bits})")

        # Flow control: if DONE, SDM is busy, don't send data
        if done:
            j112 = True
            if chunk_size > INITIAL_CHUNK:
                chunk_size //= 2
            continue

        # DONE=0: SDM ready for more data
        if j112:
            j112 = False
        elif not j123_active:
            # Only double chunk after first successful send
            chunk_size = min(chunk_size * 2, MAX_CHUNK)

        # Send data chunk from SDM's progress position
        remaining = total_bits - progress
        n = min(chunk_size, remaining)

        byte_start = progress // 8
        byte_end = (progress + n + 7) // 8
        data_slice = BitString(data[byte_start:byte_end], n)

        await config_data_shift(tap, data_slice, n)
        sent_count += 1
        retries_left = 6000  # reset timeout after successful send
        j123_active = False  # allow chunk doubling after first send

        pct = progress * 100 // total_bits
        print(f"\r  {pct}% ({progress}/{total_bits}) chunk={n} shifts={sent_count}",
              end='', flush=True)

    print()  # newline after progress


async def main():
    log.setup(level=logging.INFO)

    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <adapter-path> <j2bin-path>")
        print(f"  e.g.: {sys.argv[0]} ub3-/jtag /tmp/de25-nano-spi/data/J2.bin")
        sys.exit(1)

    root_path = sys.argv[1]
    j2bin_path = sys.argv[2]

    # Load bitstream data
    with open(j2bin_path, 'rb') as f:
        bitstream = f.read()
    print(f"Bitstream: {len(bitstream)} bytes ({len(bitstream)*8} bits)")

    # Open adapter and find TAP
    hw_root = HwRoot()
    hw_root.add_enumerator(UsbEnumerator())

    # Resolve path to the TAP.
    # Monkey-patch Agilex5.start() to skip is_configured() check
    # which hangs when the FPGA is in a transitional state after
    # a previous configuration.
    _orig_start = Agilex5.start
    async def _skip_status_start(self):
        self.logger.note("IDCODE: 0x%08x (skipping status check)",
                         self.idcode)
    Agilex5.start = _skip_status_start

    parts = root_path.strip('/').split('/')
    tap = await hw_root.child_summon(*parts, 'chain', '0')

    Agilex5.start = _orig_start  # restore

    if not isinstance(tap, Agilex5):
        print(f"Expected Agilex5 TAP, got {type(tap).__name__}")
        sys.exit(1)

    print(f"TAP: {tap.name} (IDCODE: {tap.idcode:#010x})")

    # Pre-SDM init: replicate STAPL's l39 sequence
    # IDCODE read, feature IR scans with waits, active SDM wait
    print("Pre-init...")
    idcode_val = int(await tap.IDCODE())
    await tap.run(16)

    # Feature IR scans (from STAPL l39 — may trigger config reset)
    for ir_val, wait_us in [
        (0x071, 10000),   # pulse_nconfig?
        (0x332, 10000),   # config reset?
        (0x044, 10000),   # SLD reset?
    ]:
        await tap.ir(ir_val, dr_length=10)(BitString(0, 10), read_tdo=False)
        await tap.run(16)
        await asyncio.sleep(wait_us / 1e6)

    # IR 0x281 + active wait
    await tap.ir(0x281, dr_length=10)(BitString(0, 10), read_tdo=False)
    for _ in range(200):
        await tap.run(512)
        await asyncio.sleep(512e-6)

    # Back to BYPASS
    await tap.ir(0x3FF, dr_length=1)(BitString(0, 1), read_tdo=False)
    await tap.run(16)
    print(f"  IDCODE: {idcode_val:#010x}")

    # SDM sync with retry — full flush+sync each attempt
    sdm = SdmJtag(tap)
    print("SDM sync...")
    from acrobe.component.altera.sdm import SdmTimeoutError, SdmFramingError
    for attempt in range(10):
        try:
            await sdm.sync()
            print(f"  OK (attempt {attempt + 1})")
            break
        except (SdmTimeoutError, SdmFramingError, SdmError) as e:
            print(f"  Attempt {attempt + 1}: {e}")
            # Extra flush + settle between retries
            await tap.run(512)
            await asyncio.sleep(0.05)
    else:
        print("  Sync failed after 10 attempts")
        sys.exit(1)

    # Config request — SDM takes time to respond
    print("Config request (opcode 0x05)...")
    for attempt in range(5):
        try:
            cid = sdm._id & 0xF
            sdm._id += 1
            header = (Agilex5SdmCommand.CONFIG_REQUEST & 0x7FF) \
                | ((cid & 0xF) << 24)
            await sdm._send_frame([header])
            rsp = await sdm._recv_frame(max_silent=50)
            if rsp:
                err = rsp[0] & 0x7FF
                if err:
                    raise SdmError(err,
                                   opcode=int(Agilex5SdmCommand.CONFIG_REQUEST))
            print(f"  OK (attempt {attempt + 1})")
            break
        except (SdmTimeoutError, SdmError) as e:
            print(f"  Attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(0.1)
    else:
        print("  Config request failed after 5 attempts")
        sys.exit(1)

    # Stream bitstream
    print(f"Streaming {len(bitstream)} bytes...")
    await stream_bitstream(tap, bitstream)

    # Post-config status check via SDM
    print("Checking config status...")
    from acrobe.component.altera.agilex5 import AgilexSdmClient
    client = AgilexSdmClient(sdm)

    for attempt in range(15):
        await asyncio.sleep(0.1)
        try:
            cs = await client.config_status()
            print(f"  Attempt {attempt + 1}:")
            cs.dump_pretty(lambda s: print(f"    {s}"))
            if cs.conf_done:
                print("\n  CONF_DONE asserted — configuration successful!")
                return
        except SdmError as e:
            print(f"  Attempt {attempt + 1}: {e}")

    print("\n  CONF_DONE not asserted after 15 attempts")
    sys.exit(1)


asyncio.run(main())
