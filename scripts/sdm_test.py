#!/usr/bin/env python3.13
"""Quick SDM command test.

Usage:
    python3.13 sdm_test.py <adapter-path>
    e.g.: python3.13 sdm_test.py tei-/jtag

Syncs with SDM, sends GET_IDCODE, prints result.
"""

import asyncio
import logging
import sys

from acrobe.adapter.model import HwRoot, UsbEnumerator
from acrobe.component.altera.sdm_jtag import SdmJtag
from acrobe.component.altera.sdm import SdmError
from acrobe import log


async def main():
    log.setup(level=logging.INFO)

    root_path = sys.argv[1] if len(sys.argv) > 1 else "ub3-/jtag/0/0"

    hw_root = HwRoot()
    hw_root.add_enumerator(UsbEnumerator())

    parts = root_path.strip('/').split('/')
    leaf = await hw_root.child_summon(*parts)
    await leaf.start_tree()

    # Get the Agilex 5 TAP from the discovered chain
    tap = leaf
    sdm = await tap.child_summon("sdm")

    print("\nSending GET_IDCODE...")
    sdm_idcode = await sdm.get_idcode()
    print(f"SDM IDCODE: {sdm_idcode:#010x}")
    if sdm_idcode == tap.idcode:
        print("  Matches JTAG IDCODE!")

    print("\nSending GET_CHIPID...")
    chipid = await sdm.get_chipid()
    print(f"Chip ID: {chipid:#018x}")

    print("\nSending CONFIG_STATUS...")
    cs = await sdm.config_status()
    cs.dump_pretty(print)

if __name__ == "__main__":
    asyncio.run(main())
