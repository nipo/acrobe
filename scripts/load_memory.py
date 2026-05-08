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
from acrobe.component.arm.dp import Dp
from acrobe.root import root


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

    chunk = 2 * 1024
    total = len(blob)
    print(f"Writing {total} B to 0x{address:x}...")
    t0 = time.monotonic()
    last_write = None
    transferred = 0
    for off in list(range(chunk, total, chunk)) + [0]:
        chunk_data = blob[off:off + chunk]
        cur_write = ap.mem_write(address + off, chunk_data)
        await asyncio.sleep(0)
        transferred += len(chunk_data)
        rate = transferred / (time.monotonic() - t0 + 1e-9) / 1024
        print(f"  {transferred}/{total} ({100 * transferred / total:.1f}%)  {rate:.1f} KiB/s")
        if last_write:
            await last_write
        last_write = cur_write
    await last_write

    if verify:
        print("Verify...")
        t0 = time.monotonic()
        for off in range(0, total, chunk):
            n = min(chunk, total - off)
            got = await ap.mem_read(address + off, n)
            want = blob[off:off + n]
            if got != want:
                for i, (a, b) in enumerate(zip(got, want)):
                    if a != b:
                        print(f"MISMATCH at 0x{address + off + i:x}: "
                              f"got 0x{a:02x}, want 0x{b:02x}",
                              file=sys.stderr)
                        sys.exit(2)
            done = off + n
            rate = done / (time.monotonic() - t0 + 1e-9) / 1024
            print(f"  {done}/{total} ({100 * done / total:.1f}%)  {rate:.1f} KiB/s")
        print("OK")

    await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
