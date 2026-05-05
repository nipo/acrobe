#!/usr/bin/env python3.13
"""Load a binary blob to target memory via ARM ADI.

Usage:
    python -m scripts.load_memory <root-path> <file> <address> [--verify]

Example:
    python -m scripts.load_memory \\
        wire/lure/tei/jtag/chain u-boot.bin 0x80000000 --verify
"""

import asyncio
import sys
import time

from acrobe import shutdown
from acrobe.component.arm.dp import Dp, DpRead, DpWrite, Abort
from acrobe.root import root


# Up to this many surrounding bytes printed on a verify mismatch, so
# the failure mode (e.g. memory unchanged vs partial write vs random
# corruption) is recognisable without re-running.
MISMATCH_DUMP_BYTES = 32

# Mask of CTRL/STAT bits that indicate a write/read transaction failed
# silently. STICKYERR is the canonical "your last AP transaction
# faulted" flag; STICKYORUN appears on overrun-detection-enabled DPs.
# WDATAERR catches a posted-write AP write that the chip rejected.
STICKY_MASK = Dp.STICKYERR | Dp.STICKYORUN | Dp.WDATAERR


async def check_and_clear_sticky(dp, label):
    """Read CTRL/STAT, log if any sticky-error bit is set, then ABORT
    to clear sticky bits. Returns True if any sticky was set (caller
    can decide what to do with it)."""
    stat = await dp.post(DpRead(Dp.CTRL_STAT))
    if stat & STICKY_MASK:
        bits = []
        if stat & Dp.STICKYERR:  bits.append("STICKYERR")
        if stat & Dp.STICKYORUN: bits.append("STICKYORUN")
        if stat & Dp.WDATAERR:   bits.append("WDATAERR")
        print(f"  [{label}] CTRL/STAT=0x{stat:08x} sticky: {','.join(bits)} "
              f"— clearing via ABORT", file=sys.stderr)
        await dp.post(Abort(Dp.ABORT_ALL))
        return True
    return False


def dump_mismatch_window(got, want, base_addr, mismatch_offset):
    """Print a short hex window around the first differing byte so the
    operator can see whether it's a single-byte glitch, a whole-block
    no-op, or all-zero/all-one stuck data."""
    half = MISMATCH_DUMP_BYTES // 2
    start = max(0, mismatch_offset - half)
    end = min(len(got), mismatch_offset + half)

    def hexline(buf):
        return " ".join(f"{b:02x}" for b in buf[start:end])

    print(f"  window 0x{base_addr + start:x}..0x{base_addr + end - 1:x}",
          file=sys.stderr)
    print(f"    got:  {hexline(got)}",  file=sys.stderr)
    print(f"    want: {hexline(want)}", file=sys.stderr)


async def main():
    if len(sys.argv) < 4:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    root_path = sys.argv[1]
    file_path = sys.argv[2]
    address = int(sys.argv[3], 0)
    verify = "--verify" in sys.argv[4:]

    with open(file_path, "rb") as f:
        blob = f.read()

    leaf = await root(root_path)
    await leaf.start_tree()

    dps = leaf.children_of_class(Dp, include_self=True)
    if not dps:
        print(f"No DP under {root_path!r}", file=sys.stderr)
        sys.exit(1)
    dp = dps[0]
    print(f"DP: {dp.fqdn}")
    chip = dp.chip_id()
    if chip:
        print(f"  chip: {chip}")

    ap = dp.system_memap()
    if ap is None:
        print(f"No MEM-AP under {dp.fqdn}", file=sys.stderr)
        sys.exit(1)
    print(f"MEM-AP: {ap.fqdn} (IDR=0x{ap.idr:08x})")

    # Clear any pre-existing sticky from prior sessions before measuring.
    await check_and_clear_sticky(dp, "pre")

    total = len(blob)
    print(f"Writing {total} B to 0x{address:x}...")
    t0 = time.monotonic()
    # One big mem_write — let MemAp's batcher emit one continuous
    # Write32 stream so CSW is set once and TAR is rewritten only on
    # the AP's auto-increment-window crossings (1 KiB by spec).
    # Chunking at the script level breaks that pipelining.
    await ap.mem_write(address, blob)
    elapsed = time.monotonic() - t0
    rate = total / elapsed / 1024
    print(f"  done in {elapsed:.2f} s = {rate:.1f} KiB/s")
    sticky_after_write = await check_and_clear_sticky(dp, "post-write")
    if sticky_after_write:
        print("  WARNING: writes faulted silently — verify will fail",
              file=sys.stderr)

    if verify:
        print("Verify...")
        chunk = 64 * 1024
        t0 = time.monotonic()
        all_ok = True
        for off in range(0, total, chunk):
            n = min(chunk, total - off)
            got = await ap.mem_read(address + off, n)
            want = blob[off:off + n]
            if got != want:
                # Find first mismatch within this chunk and dump a window.
                for i, (a, b) in enumerate(zip(got, want)):
                    if a != b:
                        print(f"MISMATCH at 0x{address + off + i:x}: "
                              f"got 0x{a:02x}, want 0x{b:02x}",
                              file=sys.stderr)
                        dump_mismatch_window(got, want,
                                             address + off, i)
                        break
                all_ok = False
                # Keep checking later chunks too — useful to see
                # whether failure is sparse or wholesale.
            done = off + n
            rate = done / (time.monotonic() - t0 + 1e-9) / 1024
            ok = "ok" if got == want else "FAIL"
            print(f"  {done}/{total} ({100 * done / total:.1f}%)  "
                  f"{rate:.1f} KiB/s  [{ok}]")
        await check_and_clear_sticky(dp, "post-verify")
        if all_ok:
            print("OK")
        else:
            sys.exit(2)

    await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
